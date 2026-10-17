"""End-to-end analysis pipeline: scan -> resolve -> investigate."""

from pathlib import Path

from .investigator import investigate
from .models import Candidate
from .resolver import resolve_references
from .scanner import scan_directory


def run(root: str | Path) -> list[Candidate]:
    """Run the full analysis on a directory (or a single file)."""
    files = scan_directory(root)
    strong, weak = resolve_references(files)
    return investigate(files, strong, weak)
