"""M2 --prove: prove that deleting a candidate is safe.

For every candidate found by the M1 pipeline (filtered by confidence),
delete it inside an isolated git worktree and run the project's test suite
there. If the suite stays green, the deletion is *proven* safe.

Isolation: `git worktree` creates a clean checkout at HEAD, so the user's
working tree is never modified. Deletions are AST-driven line removals
(never regexes); anything that cannot be deleted cleanly is skipped with a
reason instead of being force-deleted.

Purely local: no network, no LLM, deterministic.
"""

import ast
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from .models import CONFIDENCE_ORDER, Candidate, Symbol
from .pipeline import run

DEFAULT_TEST_CMD = "python -m pytest -x -q"
DEFAULT_TIMEOUT = 300

# ProveResult.status values.
PROVEN = "proven"  # removed it, the test suite still passes
BREAKS = "breaks"  # removed it, the test suite fails
SKIPPED = "skipped"  # the deletion could not be attempted safely


@dataclass
class ProveResult:
    """Outcome of proving a single candidate."""

    candidate: Candidate
    status: str  # PROVEN | BREAKS | SKIPPED
    reason: str = ""
    failure_summary: str = ""

    def to_dict(self) -> dict:
        payload = self.candidate.to_dict()
        payload.update(
            {
                "status": self.status,
                "reason": self.reason,
                "failure_summary": self.failure_summary,
            }
        )
        return payload


@dataclass
class ProveReport:
    """All prove outcomes for one run."""

    results: list[ProveResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        totals = {PROVEN: 0, BREAKS: 0, SKIPPED: 0}
        for result in self.results:
            totals[result.status] += 1
        return totals

    def to_dict(self, path: str, test_cmd: str) -> dict:
        totals = self.counts()
        return {
            "path": path,
            "test_cmd": test_cmd,
            "summary": {"total": len(self.results), **totals},
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# git worktree isolation
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=60
    )


def _repo_toplevel(path: Path) -> Path | None:
    """Return the repo root if `path` is inside a git work tree, else None."""
    proc = _git(["rev-parse", "--show-toplevel"], cwd=path)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def _create_worktree(repo: Path) -> Path:
    """Create a detached worktree at HEAD; caller must remove it via
    _remove_worktree. Raises RuntimeError on failure."""
    base = Path(tempfile.mkdtemp(prefix="deadcode-hunter-"))
    wt = base / "wt"
    proc = _git(["worktree", "add", "--detach", str(wt), "HEAD"], cwd=repo)
    if proc.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    return wt


def _remove_worktree(repo: Path, wt: Path) -> None:
    _git(["worktree", "remove", "--force", str(wt)], cwd=repo)
    shutil.rmtree(wt.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# AST-driven deletion
# ---------------------------------------------------------------------------


def _module_of(rel_path: str) -> str:
    """Dotted module name for a scan-root-relative path, mirroring the scanner."""
    parts = list(Path(rel_path).parts[:-1])
    stem = Path(rel_path).stem
    if stem != "__init__":
        parts.append(stem)
    return ".".join(parts)


def _bound_names(stmt: ast.AST) -> set[str]:
    """Names bound by an Assign/AnnAssign statement."""
    targets: list[ast.AST] = []
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
    elif isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    names: set[str] = set()
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return names


def _find_node(
    tree: ast.Module, module: str, symbol: Symbol
) -> ast.AST | None:
    """Locate the AST node defining `symbol`.

    Mirrors the scanner's scope/qualname bookkeeping, then disambiguates by
    source line. Returns None when the definition cannot be found uniquely
    (e.g. the worktree is at an older commit than the scanned working tree).
    """
    matches: list[ast.AST] = []

    def visit(node: ast.AST, scope: str, scope_kind: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qual = f"{scope}.{child.name}"
                if (
                    symbol.kind == "class"
                    and scope == symbol.scope
                    and child.name == symbol.name
                ):
                    matches.append(child)
                visit(child, qual, "class")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{scope}.{child.name}"
                want = "method" if scope_kind == "class" else "function"
                if (
                    symbol.kind == want
                    and scope == symbol.scope
                    and child.name == symbol.name
                ):
                    matches.append(child)
                visit(child, qual, "function")
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                if (
                    symbol.kind == "import"
                    and scope == symbol.scope
                    and child.lineno == symbol.lineno
                ):
                    for alias in child.names:
                        if (alias.asname or alias.name) == symbol.name:
                            matches.append(child)
                            break
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                if (
                    symbol.kind == "variable"
                    and scope == symbol.scope
                    and symbol.name in _bound_names(child)
                ):
                    matches.append(child)
            else:
                visit(child, scope, scope_kind)

    visit(tree, module, "module")
    exact = [m for m in matches if m.lineno == symbol.lineno]
    return exact[0] if len(exact) == 1 else None


def _deletion_span(node: ast.AST) -> tuple[int, int]:
    """Line span to delete, including decorator lines for defs/classes."""
    start = node.lineno
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for dec in node.decorator_list:
            start = min(start, dec.lineno)
    return start, node.end_lineno or node.lineno


def _sole_span(
    tree: ast.Module, node: ast.AST, start: int, end: int
) -> bool:
    """True when no *other* statement shares lines [start, end].

    Ancestors and descendants of `node` are ignored (a method's span lives
    inside its class's span, etc.).
    """
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    skip: set[ast.AST] = set(ast.walk(node))
    current: ast.AST | None = node
    while current in parents:
        current = parents[current]
        skip.add(current)
    for child in ast.walk(tree):
        if child in skip or not isinstance(child, ast.stmt):
            continue
        other_start, other_end = child.lineno, child.end_lineno or child.lineno
        if other_start <= end and start <= other_end:
            return False
    return True


def _format_alias(alias: ast.alias) -> str:
    return f"{alias.name} as {alias.asname}" if alias.asname else alias.name


def _rebuild_import(node: ast.Import | ast.ImportFrom, remaining: list[ast.alias]) -> str:
    names = ", ".join(_format_alias(a) for a in remaining)
    if isinstance(node, ast.Import):
        return f"import {names}"
    dots = "." * node.level
    module = node.module or ""
    return f"from {dots}{module} import {names}"


def _remove_node(
    tree: ast.Module, lines: list[str], node: ast.AST, symbol: Symbol
) -> tuple[list[str] | None, str]:
    """Compute new file lines with the symbol deleted.

    Returns (new_lines, "") on success, or (None, reason) when the deletion
    cannot be done cleanly.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start, end = _deletion_span(node)
        if not _sole_span(tree, node, start, end):
            return None, "definition shares lines with another statement"
        return lines[: start - 1] + lines[end:], ""

    if isinstance(node, (ast.Import, ast.ImportFrom)):
        if (node.end_lineno or node.lineno) != node.lineno:
            return None, "multi-line import statement"
        remaining = [
            a for a in node.names if (a.asname or a.name) != symbol.name
        ]
        if len(remaining) == len(node.names):
            return None, f"import binding '{symbol.name}' not found"
        idx = node.lineno - 1
        if not _sole_span(tree, node, node.lineno, node.lineno):
            return None, "import shares its line with another statement"
        if not remaining:
            return lines[:idx] + lines[idx + 1 :], ""
        line = lines[idx]
        indent = line[: len(line) - len(line.lstrip())]
        newline = "\n" if line.endswith("\n") else ""
        return (
            lines[:idx] + [indent + _rebuild_import(node, remaining) + newline] + lines[idx + 1 :],
            "",
        )

    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        bound = _bound_names(node)
        if bound != {symbol.name}:
            extra = sorted(bound - {symbol.name})
            return None, f"assignment also binds {extra}"
        start, end = node.lineno, node.end_lineno or node.lineno
        if not _sole_span(tree, node, start, end):
            return None, "assignment shares lines with another statement"
        return lines[: start - 1] + lines[end:], ""

    return None, f"unsupported node type {type(node).__name__}"


def _fill_empty_block(
    orig_lines: list[str], new_lines: list[str], node: ast.AST
) -> str | None:
    """Insert `pass` where a deletion emptied a block.

    E.g. removing the only method of a class would leave `class C:` with an
    empty body, which is a SyntaxError. Inserting `pass` at the block's indent
    is exactly what a human would do. Returns the patched source.
    """
    start = node.lineno
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for dec in node.decorator_list:
            start = min(start, dec.lineno)
    def_line = orig_lines[start - 1]
    indent = def_line[: len(def_line) - len(def_line.lstrip())]
    patched = new_lines[: start - 1] + [f"{indent}pass\n"] + new_lines[start - 1 :]
    return "".join(patched)


def _delete_symbol(path: Path, symbol: Symbol) -> tuple[bool, str]:
    """Delete a symbol's definition from `path`.

    Returns (True, "") on success, or (False, reason) when the deletion
    cannot be done cleanly -- the caller should skip the candidate.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read file: {exc}"
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"cannot parse file: {exc}"

    node = _find_node(tree, _module_of(symbol.file), symbol)
    if node is None:
        return False, f"could not uniquely locate {symbol.kind} '{symbol.name}'"

    lines = source.splitlines(keepends=True)
    new_lines, reason = _remove_node(tree, lines, node, symbol)
    if new_lines is None:
        return False, reason
    new_source = "".join(new_lines)
    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        if "expected an indented block" not in str(exc):
            return False, f"deletion breaks syntax: {exc}"
        # The deletion emptied a block (e.g. the only method of a class):
        # fill it with `pass`, exactly as a human would.
        new_source = _fill_empty_block(lines, new_lines, node)
        try:
            ast.parse(new_source)
        except SyntaxError as exc2:
            return False, f"deletion breaks syntax: {exc2}"
    try:
        path.write_text(new_source, encoding="utf-8")
    except OSError as exc:
        return False, f"cannot write file: {exc}"
    return True, ""


# ---------------------------------------------------------------------------
# test execution
# ---------------------------------------------------------------------------


def _run_tests(
    worktree: Path, test_argv: list[str], timeout: int
) -> tuple[int, str]:
    """Run the test suite in the worktree. Returns (returncode, output)."""
    try:
        proc = subprocess.run(
            test_argv, cwd=worktree, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return 124, output
    except OSError as exc:
        return 127, f"failed to launch test command: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _failure_summary(output: str, max_lines: int = 15) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])[:2000]


def _restore(worktree: Path) -> None:
    _git(["checkout", "--", "."], cwd=worktree)


def _prove_one(
    worktree: Path,
    scan_rel: Path,
    candidate: Candidate,
    test_argv: list[str],
    timeout: int,
) -> ProveResult:
    """Delete one candidate in the worktree, run the suite, restore."""
    target = worktree / scan_rel / candidate.symbol.file
    if not target.is_file():
        return ProveResult(
            candidate, SKIPPED, "file not in worktree (uncommitted changes?)"
        )
    ok, reason = _delete_symbol(target, candidate.symbol)
    if not ok:
        return ProveResult(candidate, SKIPPED, reason)
    try:
        returncode, output = _run_tests(worktree, test_argv, timeout)
    finally:
        _restore(worktree)
    if returncode == 0:
        return ProveResult(candidate, PROVEN, "tests pass without it")
    if returncode == 124:
        return ProveResult(
            candidate,
            BREAKS,
            f"test run timed out after {timeout}s",
            _failure_summary(output),
        )
    return ProveResult(
        candidate, BREAKS, "tests fail without it", _failure_summary(output)
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_prove(
    target: str | Path,
    min_confidence: str = "high",
    test_cmd: str = DEFAULT_TEST_CMD,
    timeout: int = DEFAULT_TIMEOUT,
    progress: bool = True,
) -> ProveReport:
    """Prove candidates safe to delete.

    Raises RuntimeError when the target is not a git repo, the baseline test
    suite fails, or the worktree cannot be created.
    """
    root = Path(target)
    toplevel = _repo_toplevel(root)
    if toplevel is None:
        raise RuntimeError(
            f"not a git repository: {target} ('prove' needs a git repo "
            "for isolated worktrees)"
        )
    test_argv = shlex.split(test_cmd)
    if not test_argv:
        raise RuntimeError("--test-cmd is empty")
    scan_root = root if root.is_dir() else root.parent
    scan_rel = scan_root.resolve().relative_to(toplevel.resolve())

    worktree = _create_worktree(toplevel)
    console = Console(stderr=True)
    try:
        returncode, _ = _run_tests(worktree, test_argv, timeout)
        if returncode != 0:
            raise RuntimeError(
                "baseline test suite failed in a clean worktree; "
                "fix it before using 'prove'"
            )
        threshold = CONFIDENCE_ORDER[min_confidence]
        candidates = [
            c for c in run(root) if CONFIDENCE_ORDER[c.confidence] >= threshold
        ]
        results = []
        total = len(candidates)
        for i, candidate in enumerate(candidates, 1):
            if progress:
                # stderr: keeps stdout clean for --format json consumers.
                console.print(
                    f"[{i}/{total}] proving {candidate.symbol.qualname} ...",
                    style="dim",
                )
            results.append(_prove_one(worktree, scan_rel, candidate, test_argv, timeout))
        return ProveReport(results=results)
    finally:
        _remove_worktree(toplevel, worktree)
