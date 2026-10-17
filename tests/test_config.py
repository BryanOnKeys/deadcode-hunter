"""Tests for project-local configuration (M9).

A fixture project carries its own pyproject.toml with [tool.deadcode-hunter]:
- `exclude` globs drop files from the scan;
- `entry-points` globs keep matching symbols alive;
- `min-confidence` / `test-cmd` act as defaults that CLI flags override;
- without a config file, behavior is unchanged.
"""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deadcode_hunter.cli import app
from deadcode_hunter.config import HunterConfig, load_config, matches_any

runner = CliRunner()

MOD_SRC = '''def live():
    return 1


def dead_here():
    return "dead"
'''

EXCLUDED_SRC = '''def dead_excluded():
    return "excluded"
'''

ENTRY_SRC = '''def public_api():
    return "api"


def dead_entry():
    return "dead"
'''

PYPROJECT = '''[tool.deadcode-hunter]
exclude = ["excluded.py"]
entry-points = ["entry.public_api"]
min-confidence = "high"
test-cmd = "echo config-test-cmd"
'''


@pytest.fixture
def cfg_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mod.py").write_text(MOD_SRC, encoding="utf-8")
    (proj / "excluded.py").write_text(EXCLUDED_SRC, encoding="utf-8")
    (proj / "entry.py").write_text(ENTRY_SRC, encoding="utf-8")
    (proj / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "init"],
        cwd=proj, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
        cwd=proj, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-m", "init"],
        cwd=proj, check=True, capture_output=True,
    )
    return proj


def _symbols_of(result) -> set[str]:
    payload = json.loads(result.output)
    return {c["symbol"] for c in payload["candidates"]}


def test_load_config_parses(cfg_project: Path) -> None:
    cfg = load_config(cfg_project)
    assert cfg.exclude == ["excluded.py"]
    assert cfg.entry_points == ["entry.public_api"]
    assert cfg.min_confidence == "high"
    assert cfg.test_cmd == "echo config-test-cmd"


def test_load_config_missing_file(tmp_path: Path) -> None:
    assert load_config(tmp_path) == HunterConfig()


def test_load_config_invalid_values(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.deadcode-hunter]\nmin-confidence = 'bogus'\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="min-confidence"):
        load_config(tmp_path)


def test_exclude_and_entry_points(cfg_project: Path) -> None:
    result = runner.invoke(
        app, ["scan", str(cfg_project), "--format", "json", "--min-confidence", "low"]
    )
    assert result.exit_code == 0, result.output
    symbols = _symbols_of(result)
    # excluded.py is not scanned at all
    assert not any(s.startswith("excluded.") for s in symbols)
    # entry.public_api is kept alive by the entry-points glob
    assert "entry.public_api" not in symbols
    # everything else still reported
    assert "mod.dead_here" in symbols
    assert "entry.dead_entry" in symbols


def test_config_min_confidence_default(cfg_project: Path) -> None:
    # config says min-confidence = "high"; without the flag, only high shows
    result = runner.invoke(app, ["scan", str(cfg_project), "--format", "json"])
    assert result.exit_code == 0, result.output
    high_only = _symbols_of(result)
    payload = json.loads(result.output)
    assert payload["candidates"]
    assert all(c["confidence"] == "high" for c in payload["candidates"])
    # CLI flag overrides the config value (low is a superset of high)
    result = runner.invoke(
        app, ["scan", str(cfg_project), "--format", "json", "--min-confidence", "low"]
    )
    assert result.exit_code == 0, result.output
    assert high_only <= _symbols_of(result)


def test_config_test_cmd_default_used_by_prove(cfg_project: Path) -> None:
    # the config's test-cmd is echoed into the prove table title
    result = runner.invoke(
        app, ["prove", str(cfg_project), "--format", "json", "--min-confidence", "high"]
    )
    # 'echo config-test-cmd' exits 0 -> baseline passes; prove runs
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["test_cmd"] == "echo config-test-cmd"


def test_no_config_keeps_old_defaults(tmp_path: Path) -> None:
    proj = tmp_path / "plain"
    proj.mkdir()
    (proj / "m.py").write_text("def dead():\n    return 1\n", encoding="utf-8")
    result = runner.invoke(app, ["scan", str(proj), "--format", "json"])
    assert result.exit_code == 0, result.output
    # default min-confidence for scan is still "low"
    assert json.loads(result.output)["summary"]["total"] >= 1


def test_matches_any() -> None:
    assert matches_any("tests/fixtures/a.py", ["tests/fixtures/**"])
    assert matches_any("docs/guide.md", ["docs/**"])
    assert not matches_any("src/a.py", ["docs/**"])
    assert matches_any("pkg.api.func", ["pkg.api.*"])
    assert not matches_any("pkg.other.func", ["pkg.api.*"])
