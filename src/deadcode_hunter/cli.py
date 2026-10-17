"""Command-line interface for deadcode-hunter."""

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .baseline import load_baseline, only_new
from .baseline import save_baseline as _write_baseline_file
from .cleaner import CleanPlan, apply_clean, plan_clean
from .config import HunterConfig, load_config
from .models import CONFIDENCE_ORDER, Candidate
from .pipeline import run
from .prover import (
    BREAKS,
    DEFAULT_TEST_CMD,
    DEFAULT_TIMEOUT,
    PROVEN,
    SKIPPED,
    ProveReport,
    ProveResult,
    run_prove,
)
from .report import write_report

app = typer.Typer(
    help="Find dead code in Python and Java projects — and prove it's safe to delete.",
    no_args_is_help=True,
)
console = Console()

_CONFIDENCE_STYLE = {"high": "bold red", "medium": "yellow", "low": "dim"}
_RESULT_STYLE = {PROVEN: "green", BREAKS: "bold red", SKIPPED: "dim"}


def _check_format(format: str) -> str:
    normalized = format.lower()
    if normalized not in ("table", "json"):
        raise typer.BadParameter("--format must be 'table' or 'json'")
    return normalized


def _check_confidence(min_confidence: str) -> str:
    normalized = min_confidence.lower()
    if normalized not in CONFIDENCE_ORDER:
        raise typer.BadParameter("--min-confidence must be high, medium or low")
    return normalized


def _check_lang(lang: str) -> str:
    normalized = lang.lower()
    if normalized not in ("auto", "python", "java"):
        raise typer.BadParameter("--lang must be 'auto', 'python' or 'java'")
    return normalized


def _filter_candidates(candidates: list[Candidate], min_confidence: str) -> list[Candidate]:
    threshold = CONFIDENCE_ORDER[min_confidence]
    return [c for c in candidates if CONFIDENCE_ORDER[c.confidence] >= threshold]


def _load_config_for(target: Path) -> HunterConfig:
    """Load [tool.deadcode-hunter] from the scan target's own pyproject.toml."""
    root = target if target.is_dir() else target.parent
    try:
        return load_config(root)
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)


def _resolve_opt(cli_value: str | None, cfg_value: str | None, default: str) -> str:
    """CLI flag wins, then config file, then the built-in default."""
    return cli_value or cfg_value or default


def _print_table(candidates: list[Candidate], path: str) -> None:
    table = Table(title=f"deadcode-hunter: {len(candidates)} candidate(s) in {path}")
    table.add_column("File", style="cyan", no_wrap=False)
    table.add_column("Symbol")
    table.add_column("Kind")
    table.add_column("Confidence")
    table.add_column("Evidence")
    for candidate in candidates:
        symbol = candidate.symbol
        table.add_row(
            symbol.file,
            symbol.qualname,
            symbol.kind,
            f"[{_CONFIDENCE_STYLE[candidate.confidence]}]{candidate.confidence}[/]",
            candidate.evidence_summary,
        )
    console.print(table)
    counts = {level: 0 for level in CONFIDENCE_ORDER}
    for candidate in candidates:
        counts[candidate.confidence] += 1
    console.print(
        f"high: {counts['high']}  medium: {counts['medium']}  low: {counts['low']}",
        style="dim",
    )


def _print_json(candidates: list[Candidate], path: str) -> None:
    counts = {level: 0 for level in CONFIDENCE_ORDER}
    for candidate in candidates:
        counts[candidate.confidence] += 1
    payload = {
        "path": path,
        "summary": {"total": len(candidates), **counts},
        "candidates": [c.to_dict() for c in candidates],
    }
    # NOTE: plain print, not console.print — rich would wrap long lines
    # and produce invalid JSON.
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _prove_detail(result: ProveResult) -> str:
    if result.status == BREAKS and result.failure_summary:
        last = result.failure_summary.strip().splitlines()[-1][:100]
        return f"{result.reason} :: {last}"
    return result.reason or "-"


def _print_prove_table(report: ProveReport, path: str, test_cmd: str) -> None:
    table = Table(title=f"deadcode-hunter prove: {path}  ({test_cmd})")
    table.add_column("File", style="cyan", no_wrap=False)
    table.add_column("Symbol")
    table.add_column("Result")
    table.add_column("Detail")
    for result in report.results:
        symbol = result.candidate.symbol
        table.add_row(
            symbol.file,
            symbol.qualname,
            f"[{_RESULT_STYLE[result.status]}]{result.status}[/]",
            _prove_detail(result),
        )
    console.print(table)
    counts = report.counts()
    console.print(
        f"proven: {counts[PROVEN]}  breaks: {counts[BREAKS]}  skipped: {counts[SKIPPED]}",
        style="dim",
    )


def _print_prove_json(report: ProveReport, path: str, test_cmd: str) -> None:
    # NOTE: plain print, not console.print — rich would wrap long lines
    # and produce invalid JSON.
    print(json.dumps(report.to_dict(path, test_cmd), indent=2, ensure_ascii=False))


@app.command()
def version() -> None:
    """Show the deadcode-hunter version."""
    console.print(f"deadcode-hunter {__version__}")


@app.command()
def scan(
    path: str = typer.Argument(..., help="Directory (or file) to scan for dead code"),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
    min_confidence: str | None = typer.Option(
        None,
        "--min-confidence",
        help="Minimum confidence to show: high, medium or low "
        "(default: low, or [tool.deadcode-hunter] min-confidence)",
    ),
    lang: str = typer.Option(
        "auto", "--lang", help="Language to scan: auto, python or java"
    ),
    save_baseline: str | None = typer.Option(
        None,
        "--save-baseline",
        help="Write the current candidates to a baseline JSON file and exit",
    ),
    baseline: str | None = typer.Option(
        None,
        "--baseline",
        help="Only report candidates absent from the baseline JSON file",
    ),
    fail_on_new: bool = typer.Option(
        False,
        "--fail-on-new",
        help="Exit 1 when any (new) candidates are found; for CI gates",
    ),
) -> None:
    """Scan a path for dead code and report candidates with evidence."""
    output_format = _check_format(format)
    language = _check_lang(lang)
    if save_baseline is not None and baseline is not None:
        raise typer.BadParameter("use only one of --save-baseline and --baseline")
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"path does not exist: {path}")
    cfg = _load_config_for(target)
    confidence = _check_confidence(
        _resolve_opt(min_confidence, cfg.min_confidence, "low")
    )
    try:
        candidates = _filter_candidates(
            run(target, lang=language, config=cfg), confidence
        )
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    if save_baseline is not None:
        count = _write_baseline_file(candidates, save_baseline)
        console.print(
            f"[green]Baseline saved to {save_baseline}[/green] ({count} candidates)"
        )
        return
    if baseline is not None:
        try:
            known = load_baseline(baseline)
        except RuntimeError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=1)
        candidates = only_new(candidates, known)
    if output_format == "json":
        _print_json(candidates, path)
    else:
        _print_table(candidates, path)
    if fail_on_new and candidates:
        raise typer.Exit(code=1)


@app.command()
def prove(
    path: str = typer.Argument(
        ..., help="Directory (or file) whose candidates should be proven"
    ),
    min_confidence: str | None = typer.Option(
        None,
        "--min-confidence",
        help="Minimum confidence to prove: high, medium or low "
        "(default: high, or [tool.deadcode-hunter] min-confidence)",
    ),
    test_cmd: str | None = typer.Option(
        None,
        "--test-cmd",
        help="Test command, run in an isolated git worktree after each deletion "
        f"(default: '{DEFAULT_TEST_CMD}', or [tool.deadcode-hunter] test-cmd)",
    ),
    timeout: int = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", help="Per-test-run timeout in seconds"
    ),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
    lang: str = typer.Option(
        "auto", "--lang", help="Language to scan: auto, python or java"
    ),
) -> None:
    """Prove candidates safe to delete: remove each in an isolated git
    worktree and run the test suite. The working tree is never modified."""
    output_format = _check_format(format)
    language = _check_lang(lang)
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"path does not exist: {path}")
    cfg = _load_config_for(target)
    confidence = _check_confidence(
        _resolve_opt(min_confidence, cfg.min_confidence, "high")
    )
    resolved_test_cmd = _resolve_opt(test_cmd, cfg.test_cmd, DEFAULT_TEST_CMD)
    try:
        report = run_prove(
            target,
            min_confidence=confidence,
            test_cmd=resolved_test_cmd,
            timeout=timeout,
            progress=(output_format == "table"),
            lang=language,
            config=cfg,
        )
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    if output_format == "json":
        _print_prove_json(report, path, resolved_test_cmd)
    else:
        _print_prove_table(report, path, resolved_test_cmd)


def main() -> None:
    """Console entry point."""
    app()


def _print_clean_plan_table(plan: CleanPlan, path: str) -> None:
    table = Table(
        title=f"deadcode-hunter clean: {len(plan.proven)} proven-safe deletion(s) in {path}"
    )
    table.add_column("File", style="cyan", no_wrap=False)
    table.add_column("Symbol")
    table.add_column("Kind")
    for candidate in plan.proven:
        symbol = candidate.symbol
        table.add_row(symbol.file, symbol.qualname, symbol.kind)
    console.print(table)


@app.command()
def clean(
    path: str = typer.Argument(
        ..., help="Directory (or file) to clean dead code from"
    ),
    min_confidence: str | None = typer.Option(
        None,
        "--min-confidence",
        help="Minimum confidence to clean: high, medium or low "
        "(default: high, or [tool.deadcode-hunter] min-confidence)",
    ),
    test_cmd: str | None = typer.Option(
        None,
        "--test-cmd",
        help="Test command, run in an isolated git worktree to prove each deletion "
        f"(default: '{DEFAULT_TEST_CMD}', or [tool.deadcode-hunter] test-cmd)",
    ),
    timeout: int = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", help="Per-test-run timeout in seconds"
    ),
    lang: str = typer.Option(
        "auto", "--lang", help="Language to scan: auto, python or java"
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Apply deletions without asking for confirmation"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be deleted without changing any files",
    ),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
) -> None:
    """Find dead code, prove each deletion safe in an isolated git worktree,
    and apply the proven deletions to the working tree.

    The working tree must be clean (commit or stash first). Changes are left
    uncommitted for you to review with `git diff`.
    """
    output_format = _check_format(format)
    language = _check_lang(lang)
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"path does not exist: {path}")
    cfg = _load_config_for(target)
    confidence = _check_confidence(
        _resolve_opt(min_confidence, cfg.min_confidence, "high")
    )
    resolved_test_cmd = _resolve_opt(test_cmd, cfg.test_cmd, DEFAULT_TEST_CMD)
    try:
        plan = plan_clean(
            target,
            min_confidence=confidence,
            test_cmd=resolved_test_cmd,
            timeout=timeout,
            lang=language,
            config=cfg,
        )
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    if not plan.proven:
        console.print("[green]Nothing proven safe to delete.[/green]")
        return
    if output_format == "json":
        # NOTE: plain print, not console.print — rich would wrap long lines
        # and produce invalid JSON.
        print(
            json.dumps(
                {
                    "path": path,
                    "dry_run": dry_run,
                    "planned": [c.to_dict() for c in plan.proven],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        _print_clean_plan_table(plan, path)
    if dry_run:
        console.print(
            "[yellow]Dry run: no files were changed.[/yellow]", style="yellow"
        )
        return
    if not yes:
        # NOTE: pairs best with --yes when scripting; the prompt goes to
        # stdout alongside --format json output.
        typer.confirm(
            f"Delete these {len(plan.proven)} symbol(s) from the working tree?",
            abort=True,
        )
    result = apply_clean(plan)
    if output_format == "json":
        print(
            json.dumps(
                {
                    "path": path,
                    "deleted": [c.to_dict() for c in result.deleted],
                    "failed": [
                        {"candidate": c.to_dict(), "reason": reason}
                        for c, reason in result.failed
                    ],
                    "diff_stat": result.diff_stat,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        console.print(
            f"[green]deleted: {len(result.deleted)}[/green]"
            + (
                f"  [red]failed: {len(result.failed)}[/red]"
                if result.failed
                else ""
            )
        )
        for candidate, reason in result.failed:
            console.print(
                f"  [red]![/red] {candidate.symbol.qualname}: {reason}"
            )
        if result.diff_stat:
            console.print("[dim]git diff --stat:[/dim]")
            print(result.diff_stat)
        console.print(
            "[dim]Changes are uncommitted — review with `git diff` and commit when happy.[/dim]"
        )


@app.command()
def report(
    path: str = typer.Argument(..., help="Directory (or file) to scan and report on"),
    output: str = typer.Option(
        "report.html", "--output", help="Output HTML file path"
    ),
    prove_json: str | None = typer.Option(
        None,
        "--prove-json",
        help="Path to a 'prove --format json' output file; its proven/breaks "
        "results are merged into the report",
    ),
    lang: str = typer.Option(
        "auto", "--lang", help="Language to scan: auto, python or java"
    ),
) -> None:
    """Generate a self-contained HTML report of dead-code candidates.

    A single HTML file with inline CSS/JS (no external dependencies):
    summary stats, a clickable file tree, per-symbol evidence, expandable
    source snippets with dead lines highlighted, and a symbol search box.
    """
    language = _check_lang(lang)
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"path does not exist: {path}")
    if prove_json is not None and not Path(prove_json).exists():
        raise typer.BadParameter(f"prove json does not exist: {prove_json}")
    cfg = _load_config_for(target)
    try:
        candidates = run(target, lang=language, config=cfg)
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    # Mirror pipeline.run: for a single file the scan root is its parent dir.
    root = target if target.is_dir() else target.parent
    out = write_report(candidates, root, output, prove_json=prove_json, path_label=path)
    console.print(
        f"[green]Report written to {out}[/green] ({len(candidates)} candidates)"
    )


if __name__ == "__main__":
    main()
