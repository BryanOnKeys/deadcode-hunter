"""CLI tests: table/json output and confidence filtering."""

import json
from pathlib import Path

from typer.testing import CliRunner

from deadcode_hunter.cli import app

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample_repo")
runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "deadcode-hunter" in result.output


def test_scan_table_lists_high_candidates():
    result = runner.invoke(app, ["scan", FIXTURE])
    assert result.exit_code == 0
    assert "dead.dead_func" in result.output
    assert "high" in result.output


def test_scan_json_is_machine_readable():
    result = runner.invoke(app, ["scan", FIXTURE, "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["summary"]["high"] > 0
    symbols = {c["symbol"] for c in payload["candidates"]}
    assert "dead.dead_func" in symbols
    assert "dynamic.fetch" in symbols
    candidate = next(c for c in payload["candidates"] if c["symbol"] == "dead.dead_func")
    assert candidate["confidence"] == "high"
    assert candidate["evidence"]


def test_min_confidence_filters():
    result = runner.invoke(
        app, ["scan", FIXTURE, "--format", "json", "--min-confidence", "high"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["candidates"]
    assert {c["confidence"] for c in payload["candidates"]} == {"high"}

    result = runner.invoke(
        app, ["scan", FIXTURE, "--format", "json", "--min-confidence", "low"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert {c["confidence"] for c in payload["candidates"]} == {"high", "medium", "low"}


def test_scan_missing_path_fails_cleanly():
    result = runner.invoke(app, ["scan", "/nonexistent/path"])
    assert result.exit_code != 0
