"""Tests for `deadcode-hunter prove` (M2).

Builds a small git fixture repo with its own pytest suite, then proves:
- deleting dead code keeps the suite green  -> "proven"
- deleting live code breaks the suite       -> "breaks"
- non-git directories are rejected
- the git worktree is always cleaned up
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deadcode_hunter import prover
from deadcode_hunter.cli import app
from deadcode_hunter.models import Candidate, Symbol

runner = CliRunner()
TEST_CMD = f"{sys.executable} -m pytest -x -q"

MODULE_SRC = '''"""Fixture module for prove tests."""

import os
from math import ceil, floor

LIVE_VALUE = 21


def live_func():
    return LIVE_VALUE * 2


def dead_func():
    return "dead"


class DeadClass:
    def run(self):
        return 1


def uses_floor():
    return floor(2.5)
'''

TEST_SRC = '''from mymod import live_func, uses_floor, LIVE_VALUE


def test_live():
    assert live_func() == 42


def test_floor():
    assert uses_floor() == 2


def test_const():
    assert LIVE_VALUE == 21
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def prove_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mymod.py").write_text(MODULE_SRC, encoding="utf-8")
    (repo / "test_mymod.py").write_text(TEST_SRC, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixture")
    return repo


def _worktree_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return len([b for b in out.strip().split("\n\n") if b.strip()])


def test_prove_dead_code_is_proven(prove_repo: Path):
    result = runner.invoke(
        app, ["prove", str(prove_repo), "--test-cmd", TEST_CMD, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["total"] == 5
    assert payload["summary"]["proven"] == 5
    assert payload["summary"]["breaks"] == 0
    assert payload["summary"]["skipped"] == 0
    assert {r["status"] for r in payload["results"]} == {"proven"}


def test_prove_live_code_breaks_tests(prove_repo: Path):
    tree = ast.parse(MODULE_SRC)
    node = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "live_func"
    )
    symbol = Symbol(
        sid=1,
        name="live_func",
        kind="function",
        qualname="mymod.live_func",
        scope="mymod",
        file="mymod.py",
        lineno=node.lineno,
        end_lineno=node.end_lineno or node.lineno,
    )
    candidate = Candidate(symbol=symbol, confidence="high", strong_refs=0, weak_refs=0)
    worktree = prover._create_worktree(prove_repo)
    try:
        result = prover._prove_one(
            worktree, Path("."), candidate, [sys.executable, "-m", "pytest", "-x", "-q"], 120
        )
    finally:
        prover._remove_worktree(prove_repo, worktree)
    assert result.status == "breaks"
    assert result.failure_summary


def test_prove_rejects_non_git_dir(tmp_path: Path):
    result = runner.invoke(app, ["prove", str(tmp_path)])
    assert result.exit_code != 0


def test_prove_cleans_up_worktree(prove_repo: Path):
    assert _worktree_count(prove_repo) == 1
    result = runner.invoke(
        app, ["prove", str(prove_repo), "--test-cmd", TEST_CMD, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    assert _worktree_count(prove_repo) == 1


def test_prove_table_output(prove_repo: Path):
    result = runner.invoke(app, ["prove", str(prove_repo), "--test-cmd", TEST_CMD])
    assert result.exit_code == 0, result.output
    assert "proven" in result.output
