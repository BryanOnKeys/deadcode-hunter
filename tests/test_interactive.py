"""Tests for `deadcode-hunter clean --interactive` (M13).

Uses typer's CliRunner with fed stdin; the TTY check is patched because
CliRunner's stdin is never a real terminal.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from deadcode_hunter import cli as cli_module
from deadcode_hunter.cli import app

runner = CliRunner()
TEST_CMD = f"{sys.executable} -m pytest -x -q"

MODULE_SRC = '''def live():
    return 1


def dead_a():
    return 2


def dead_b():
    return 3
'''

TEST_SRC = '''from m import live


def test_live():
    assert live() == 1
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text(MODULE_SRC, encoding="utf-8")
    (repo / "test_m.py").write_text(TEST_SRC, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _invoke_interactive(repo: Path, stdin_text: str):
    with patch.object(cli_module, "_stdin_is_tty", return_value=True):
        return runner.invoke(
            app,
            ["clean", str(repo), "-i", "--test-cmd", TEST_CMD],
            input=stdin_text,
        )


def test_interactive_selective_delete(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # Candidates come in definition order: dead_a, dead_b.
    # Keep dead_a (y), skip dead_b (n).
    result = _invoke_interactive(repo, "y\nn\n")
    assert result.exit_code == 0, result.output
    src = (repo / "m.py").read_text(encoding="utf-8")
    assert "def dead_a" not in src
    assert "def dead_b" in src
    assert "def live" in src
    assert "deleted: 1" in result.output


def test_interactive_all_remaining(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = _invoke_interactive(repo, "a\n")
    assert result.exit_code == 0, result.output
    src = (repo / "m.py").read_text(encoding="utf-8")
    assert "def dead_a" not in src
    assert "def dead_b" not in src
    assert "deleted: 2" in result.output


def test_interactive_quit_aborts_everything(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = _invoke_interactive(repo, "q\n")
    assert result.exit_code == 0, result.output
    src = (repo / "m.py").read_text(encoding="utf-8")
    assert "def dead_a" in src and "def dead_b" in src
    assert "Nothing selected" in result.output


def test_interactive_skip_all_changes_nothing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = _invoke_interactive(repo, "n\nn\n")
    assert result.exit_code == 0, result.output
    src = (repo / "m.py").read_text(encoding="utf-8")
    assert "def dead_a" in src and "def dead_b" in src


def test_interactive_rejects_non_tty(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # No TTY patch: CliRunner's stdin is not a terminal.
    result = runner.invoke(
        app, ["clean", str(repo), "-i", "--test-cmd", TEST_CMD]
    )
    assert result.exit_code == 1
    assert "needs a terminal" in result.output


def test_interactive_conflicts_with_yes_and_dry_run(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    for extra in (["--yes"], ["--dry-run"]):
        result = runner.invoke(
            app, ["clean", str(repo), "-i", *extra, "--test-cmd", TEST_CMD]
        )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output
