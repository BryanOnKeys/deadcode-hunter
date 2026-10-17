"""Tests for baseline mode (M8).

Covers the intended CI workflow:
- save a baseline, add new dead code, rescan with --baseline -> only the
  new candidates are reported;
- --fail-on-new exits 1 with new candidates and 0 without;
- a corrupt baseline file produces a clear error.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from deadcode_hunter.cli import app

runner = CliRunner()

V1 = '''def live():
    return 1


def dead_v1():
    return "v1"


def main():
    return live()


if __name__ == "__main__":
    main()
'''

V2_EXTRA = '''\n\ndef dead_v2():\n    return "v2"\n'''


def _write_project(root: Path) -> None:
    (root / "mod.py").write_text(V1, encoding="utf-8")


def _invoke(*args: str):
    return runner.invoke(app, ["scan", *args])


def test_baseline_reports_only_new(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project(proj)
    baseline = tmp_path / "baseline.json"

    saved = _invoke(str(proj), "--save-baseline", str(baseline),
                    "--min-confidence", "high")
    assert saved.exit_code == 0, saved.output
    assert "Baseline saved" in saved.output
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["candidates"]) == 1

    # add new dead code, rescan with the baseline: only the newcomer shows
    with open(proj / "mod.py", "a", encoding="utf-8") as fh:
        fh.write(V2_EXTRA)
    rescan = _invoke(str(proj), "--baseline", str(baseline),
                     "--min-confidence", "high", "--format", "json")
    assert rescan.exit_code == 0, rescan.output
    payload = json.loads(rescan.output)
    assert payload["summary"]["total"] == 1
    assert payload["candidates"][0]["symbol"] == "mod.dead_v2"


def test_baseline_no_new_is_quiet(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project(proj)
    baseline = tmp_path / "baseline.json"
    _invoke(str(proj), "--save-baseline", str(baseline),
            "--min-confidence", "high")
    rescan = _invoke(str(proj), "--baseline", str(baseline),
                     "--min-confidence", "high", "--format", "json")
    assert rescan.exit_code == 0, rescan.output
    assert json.loads(rescan.output)["summary"]["total"] == 0


def test_fail_on_new_exit_codes(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project(proj)

    # no baseline: fails while any candidates exist at the filter level
    assert _invoke(str(proj), "--fail-on-new",
                   "--min-confidence", "high").exit_code == 1

    baseline = tmp_path / "baseline.json"
    _invoke(str(proj), "--save-baseline", str(baseline),
            "--min-confidence", "high")
    # nothing new -> exit 0
    assert _invoke(str(proj), "--baseline", str(baseline),
                   "--fail-on-new",
                   "--min-confidence", "high").exit_code == 0
    # something new -> exit 1
    with open(proj / "mod.py", "a", encoding="utf-8") as fh:
        fh.write(V2_EXTRA)
    assert _invoke(str(proj), "--baseline", str(baseline),
                   "--fail-on-new",
                   "--min-confidence", "high").exit_code == 1


def test_corrupt_baseline_errors_clearly(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project(proj)

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = _invoke(str(proj), "--baseline", str(bad))
    assert result.exit_code == 1
    assert "invalid baseline file" in result.output

    wrong_shape = tmp_path / "shape.json"
    wrong_shape.write_text(json.dumps({"version": 1}), encoding="utf-8")
    result = _invoke(str(proj), "--baseline", str(wrong_shape))
    assert result.exit_code == 1
    assert "invalid baseline file" in result.output

    missing = tmp_path / "nope.json"
    result = _invoke(str(proj), "--baseline", str(missing))
    assert result.exit_code == 1
    assert "cannot read baseline file" in result.output


def test_save_and_baseline_are_mutually_exclusive(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project(proj)
    result = _invoke(str(proj), "--save-baseline", "a.json",
                     "--baseline", "b.json")
    assert result.exit_code != 0
