"""Tests for the M3 HTML report command."""

import json
from html.parser import HTMLParser
from pathlib import Path

from typer.testing import CliRunner

from deadcode_hunter.cli import app
from deadcode_hunter.pipeline import run

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample_repo")
runner = CliRunner()


class _WellformedChecker(HTMLParser):
    """Minimal well-formedness check: tags must nest properly."""

    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"mismatched </{tag}>")


def _generate(tmp_path: Path, *extra_args: str) -> Path:
    out = tmp_path / "report.html"
    result = runner.invoke(app, ["report", FIXTURE, "--output", str(out), *extra_args])
    assert result.exit_code == 0, result.output
    assert out.exists()
    return out


def test_report_generates_valid_html(tmp_path):
    out = _generate(tmp_path)
    content = out.read_text(encoding="utf-8")
    assert content.startswith("<!DOCTYPE html>")
    assert content.rstrip().endswith("</html>")
    checker = _WellformedChecker()
    checker.feed(content)
    assert checker.errors == [], checker.errors
    assert checker.stack == [], checker.stack


def test_report_contains_expected_symbols_and_summary(tmp_path):
    out = _generate(tmp_path)
    content = out.read_text(encoding="utf-8")
    # known dead symbols from the fixture repo
    assert "dead.dead_func" in content
    assert "dead.DeadClass" in content
    # summary numbers match the pipeline output
    candidates = run(Path(FIXTURE))
    total = len(candidates)
    assert total > 0
    assert f'id="stat-total">{total}<' in content
    high = sum(1 for c in candidates if c.confidence == "high")
    assert f'id="stat-high">{high}<' in content
    # confidence badges are color-coded
    assert "conf-high" in content
    # evidence chain and dead-line highlighting are present
    assert "Evidence" in content
    assert 'class="line dead"' in content


def test_report_has_no_external_dependencies(tmp_path):
    out = _generate(tmp_path)
    content = out.read_text(encoding="utf-8").lower()
    assert 'src="http' not in content
    assert 'href="http' not in content
    assert "@import" not in content
    assert "cdn" not in content


def test_report_escapes_source_code(tmp_path):
    out = _generate(tmp_path)
    content = out.read_text(encoding="utf-8")
    # a raw "<script>" from source must never appear unescaped in the page
    # (the only script tag allowed is our own inline one)
    assert content.count("<script>") == 1
    assert content.count("</script>") == 1


def _fake_prove_json(tmp_path: Path) -> Path:
    payload = {
        "path": FIXTURE,
        "test_cmd": "pytest -q",
        "summary": {"total": 1, "proven": 1, "breaks": 0, "skipped": 0},
        "results": [
            {
                "file": "dead.py",
                "symbol": "dead.dead_func",
                "name": "dead_func",
                "kind": "function",
                "confidence": "high",
                "lineno": 8,
                "strong_refs": 0,
                "weak_refs": 0,
                "in_test_file": False,
                "evidence": ["defined at dead.py:8"],
                "status": "proven",
                "reason": "test suite passes after deletion",
                "failure_summary": None,
            }
        ],
    }
    prove_path = tmp_path / "prove.json"
    prove_path.write_text(json.dumps(payload), encoding="utf-8")
    return prove_path


def test_report_merges_prove_json(tmp_path):
    prove_path = _fake_prove_json(tmp_path)
    out = _generate(tmp_path, "--prove-json", str(prove_path))
    content = out.read_text(encoding="utf-8")
    # proven badge on the merged candidate card
    assert "prove-proven" in content
    assert "proven \u2713" in content
    # prove reason is shown
    assert "test suite passes after deletion" in content
    # prove counts appear in the summary
    assert 'id="stat-proven">1<' in content
    assert 'id="stat-breaks">0<' in content
    assert 'id="stat-skipped">0<' in content


def test_report_without_prove_json_has_no_prove_badges(tmp_path):
    out = _generate(tmp_path)
    content = out.read_text(encoding="utf-8")
    assert 'class="badge prove-proven"' not in content
    assert 'id="stat-proven"' not in content


def test_report_missing_path_fails_cleanly():
    result = runner.invoke(app, ["report", "/nonexistent/path", "--output", "x.html"])
    assert result.exit_code != 0


def test_report_missing_prove_json_fails_cleanly(tmp_path):
    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        ["report", FIXTURE, "--output", str(out), "--prove-json", "/nonexistent/p.json"],
    )
    assert result.exit_code != 0
