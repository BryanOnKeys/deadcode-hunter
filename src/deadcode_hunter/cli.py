"""Command-line interface for deadcode-hunter."""

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
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
    help="Find dead code in Python projects — and prove it's safe to delete.",
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


def _filter_candidates(candidates: list[Candidate], min_confidence: str) -> list[Candidate]:
    threshold = CONFIDENCE_ORDER[min_confidence]
    return [c for c in candidates if CONFIDENCE_ORDER[c.confidence] >= threshold]


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
    min_confidence: str = typer.Option(
        "low", "--min-confidence", help="Minimum confidence to show: high, medium or low"
    ),
) -> None:
    """Scan a path for dead code and report candidates with evidence."""
    output_format = _check_format(format)
    confidence = _check_confidence(min_confidence)
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"path does not exist: {path}")
    candidates = _filter_candidates(run(target), confidence)
    if output_format == "json":
        _print_json(candidates, path)
    else:
        _print_table(candidates, path)


@app.command()
def prove(
    path: str = typer.Argument(
        ..., help="Directory (or file) whose candidates should be proven"
    ),
    min_confidence: str = typer.Option(
        "high", "--min-confidence", help="Minimum confidence to prove: high, medium or low"
    ),
    test_cmd: str = typer.Option(
        DEFAULT_TEST_CMD,
        "--test-cmd",
        help="Test command, run in an isolated git worktree after each deletion",
    ),
    timeout: int = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", help="Per-test-run timeout in seconds"
    ),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
) -> None:
    """Prove candidates safe to delete: remove each in an isolated git
    worktree and run the test suite. The working tree is never modified."""
    output_format = _check_format(format)
    confidence = _check_confidence(min_confidence)
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"path does not exist: {path}")
    try:
        report = run_prove(
            target,
            min_confidence=confidence,
            test_cmd=test_cmd,
            timeout=timeout,
            progress=(output_format == "table"),
        )
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    if output_format == "json":
        _print_prove_json(report, path, test_cmd)
    else:
        _print_prove_table(report, path, test_cmd)


def main() -> None:
    """Console entry point."""
    app()


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
) -> None:
    """Generate a self-contained HTML report of dead-code candidates.

    A single HTML file with inline CSS/JS (no external dependencies):
    summary stats, a clickable file tree, per-symbol evidence, expandable
    source snippets with dead lines highlighted, and a symbol search box.
    """
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"path does not exist: {path}")
    if prove_json is not None and not Path(prove_json).exists():
        raise typer.BadParameter(f"prove json does not exist: {prove_json}")
    candidates = run(target)
    # Mirror pipeline.run: for a single file the scan root is its parent dir.
    root = target if target.is_dir() else target.parent
    out = write_report(candidates, root, output, prove_json=prove_json, path_label=path)
    console.print(
        f"[green]Report written to {out}[/green] ({len(candidates)} candidates)"
    )


if __name__ == "__main__":
    main()
