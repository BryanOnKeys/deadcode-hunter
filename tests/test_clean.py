"""Tests for `deadcode-hunter clean` (M6).

Builds a small git fixture repo with its own pytest suite, then checks:
- `--yes` deletes the proven-safe dead code and the suite still passes;
- a dirty working tree is refused with a clear message;
- `--dry-run` changes nothing;
- declining the confirmation prompt aborts without changes;
- non-git directories are rejected.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deadcode_hunter.cli import app

runner = CliRunner()
TEST_CMD = f"{sys.executable} -m pytest -x -q"

MODULE_SRC = '''"""Fixture module for clean tests."""


def live_func():
    return 1


def dead_func():
    return "dead"


class DeadClass:
    def run(self):
        return 2
'''

TEST_SRC = '''from mymod import live_func


def test_live():
    assert live_func() == 1
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mymod.py").write_text(MODULE_SRC, encoding="utf-8")
    (repo / "test_mymod.py").write_text(TEST_SRC, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--short"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def test_clean_yes_deletes_dead_code(clean_repo: Path) -> None:
    result = runner.invoke(
        app, ["clean", str(clean_repo), "--yes", "--test-cmd", TEST_CMD]
    )
    assert result.exit_code == 0, result.output
    assert "deleted: 3" in result.output  # dead_func, DeadClass (+ its method)
    src = (clean_repo / "mymod.py").read_text(encoding="utf-8")
    assert "dead_func" not in src
    assert "DeadClass" not in src
    assert "live_func" in src
    # the change is left uncommitted in the working tree
    assert "mymod.py" in _status(clean_repo)
    # the suite still passes after the deletion
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=clean_repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_clean_dirty_tree_refused(clean_repo: Path) -> None:
    (clean_repo / "mymod.py").write_text(
        (clean_repo / "mymod.py").read_text(encoding="utf-8") + "\n# dirty\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["clean", str(clean_repo), "--yes", "--test-cmd", TEST_CMD]
    )
    assert result.exit_code == 1
    assert "uncommitted changes" in result.output


def test_clean_dry_run_changes_nothing(clean_repo: Path) -> None:
    result = runner.invoke(
        app, ["clean", str(clean_repo), "--dry-run", "--test-cmd", TEST_CMD]
    )
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "dead_func" in (clean_repo / "mymod.py").read_text(encoding="utf-8")
    assert _status(clean_repo) == ""


def test_clean_decline_aborts(clean_repo: Path) -> None:
    result = runner.invoke(
        app,
        ["clean", str(clean_repo), "--test-cmd", TEST_CMD],
        input="n\n",
    )
    assert result.exit_code == 1  # typer.confirm(abort=True)
    assert "dead_func" in (clean_repo / "mymod.py").read_text(encoding="utf-8")
    assert _status(clean_repo) == ""


def test_clean_not_a_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "m.py").write_text("def dead():\n    return 1\n", encoding="utf-8")
    result = runner.invoke(app, ["clean", str(plain), "--yes"])
    assert result.exit_code == 1
    assert "not a git repository" in result.output
