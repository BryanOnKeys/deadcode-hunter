"""M6 `clean`: scan -> prove -> apply proven-safe deletions to the working tree.

`prove` shows that deleting a candidate is safe; `clean` goes one step
further and actually performs the deletions that were proven safe, leaving
the changes uncommitted in the working tree for the user to review and
commit.

Safety rails:
- the target must be inside a git repository;
- the working tree must be clean (``git status`` empty) — otherwise refuse,
  because proving happens at HEAD and deletions must apply to identical
  content;
- deletions reuse ``prover._delete_symbol`` (AST-driven, never regexes);
  anything that cannot be deleted cleanly is reported, not forced;
- ``--dry-run`` previews without touching anything;
- without ``--yes`` the user confirms the deletion list first.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .config import HunterConfig
from .models import Candidate
from .prover import (
    DEFAULT_TEST_CMD,
    DEFAULT_TIMEOUT,
    PROVEN,
    _delete_symbol,
    _git,
    _repo_toplevel,
    run_prove,
)


@dataclass
class CleanPlan:
    """Proven-safe deletions waiting for confirmation."""

    toplevel: Path  # repo root
    scan_rel: Path  # scan root, relative to the repo root
    proven: list[Candidate]


@dataclass
class CleanResult:
    """Outcome of applying a CleanPlan to the working tree."""

    deleted: list[Candidate] = field(default_factory=list)
    failed: list[tuple[Candidate, str]] = field(default_factory=list)
    diff_stat: str = ""


def _working_tree_clean(repo: Path) -> bool:
    proc = _git(["status", "--porcelain"], cwd=repo)
    return proc.returncode == 0 and not proc.stdout.strip()


def plan_clean(
    target: str | Path,
    min_confidence: str = "high",
    test_cmd: str = DEFAULT_TEST_CMD,
    timeout: int = DEFAULT_TIMEOUT,
    lang: str = "auto",
    config: HunterConfig | None = None,
) -> CleanPlan:
    """Scan and prove, returning the deletions that are safe to apply.

    Raises RuntimeError when the target is not a git repo, the working tree
    is dirty, or the baseline test suite fails. Nothing is modified.
    """
    root = Path(target)
    toplevel = _repo_toplevel(root)
    if toplevel is None:
        raise RuntimeError(
            f"not a git repository: {target} ('clean' needs a git repo)"
        )
    if not _working_tree_clean(toplevel):
        raise RuntimeError(
            "working tree has uncommitted changes — commit or stash them "
            "before 'clean' (deletions must apply to content identical to HEAD)"
        )
    report = run_prove(
        target,
        min_confidence=min_confidence,
        test_cmd=test_cmd,
        timeout=timeout,
        progress=False,
        lang=lang,
        config=config,
    )
    proven = [r.candidate for r in report.results if r.status == PROVEN]
    scan_root = root if root.is_dir() else root.parent
    scan_rel = scan_root.resolve().relative_to(toplevel.resolve())
    return CleanPlan(toplevel=toplevel, scan_rel=scan_rel, proven=proven)


def apply_clean(plan: CleanPlan) -> CleanResult:
    """Apply a CleanPlan's deletions to the working tree files.

    Returns which deletions succeeded; failures are collected with reasons
    instead of aborting the whole run. A candidate whose enclosing scope was
    already deleted (e.g. a method of a deleted class) counts as deleted —
    it went away with its parent.
    """
    result = CleanResult()
    deleted_scopes: set[str] = set()
    # Delete bottom-up: removing a later span never shifts the line numbers
    # of earlier symbols, so every candidate's recorded lineno stays valid.
    # (prove deletes each candidate in a fresh worktree, but clean applies
    # them sequentially to the same working tree.)
    ordered = sorted(
        plan.proven,
        key=lambda c: (c.symbol.file, c.symbol.lineno),
        reverse=True,
    )
    for candidate in ordered:
        qual = candidate.symbol.qualname
        parts = qual.split(".")
        if any(
            ".".join(parts[:i]) in deleted_scopes for i in range(1, len(parts))
        ):
            # An ancestor (e.g. the enclosing class) was already deleted,
            # so this symbol is gone too.
            result.deleted.append(candidate)
            deleted_scopes.add(qual)
            continue
        fpath = plan.toplevel / plan.scan_rel / candidate.symbol.file
        ok, reason = _delete_symbol(fpath, candidate.symbol)
        if ok:
            result.deleted.append(candidate)
            deleted_scopes.add(qual)
        else:
            result.failed.append((candidate, reason))
    proc = _git(["diff", "--stat"], cwd=plan.toplevel)
    result.diff_stat = proc.stdout.strip() if proc.returncode == 0 else ""
    return result
