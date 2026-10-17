"""Dogfood: the scanner runs on its own codebase and must report zero
high-confidence dead code. If this fails, fix the code (or the scanner)
before shipping."""

from pathlib import Path

from deadcode_hunter.pipeline import run as scan

PROJECT_SRC = Path(__file__).parent.parent / "src" / "deadcode_hunter"


def test_dogfood_zero_high_confidence():
    candidates = scan(PROJECT_SRC)
    high = [c for c in candidates if c.confidence == "high"]
    assert not high, (
        "dogfood failed: high-confidence dead code in our own codebase:\n"
        + "\n".join(f"  {c.symbol.qualname} ({c.symbol.file}:{c.symbol.lineno})" for c in high)
    )


def test_dogfood_completes_without_crash():
    # all supported confidence levels are representable on our own code
    candidates = scan(PROJECT_SRC)
    assert {c.confidence for c in candidates} <= {"high", "medium", "low"}
