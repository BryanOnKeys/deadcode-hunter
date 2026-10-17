"""Tests for the Java scanner (tree-sitter based)."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from deadcode_hunter.models import HIGH, MEDIUM
from deadcode_hunter.pipeline import run
from deadcode_hunter.prover import PROVEN, run_prove
from deadcode_hunter.scanner_java import delete_java_symbol, scan_java_file

FIXTURE = Path(__file__).parent / "fixtures" / "sample_java"


def _candidates(path=FIXTURE, lang="auto"):
    return {c.symbol.qualname: c for c in run(path, lang=lang)}


def test_java_high_set():
    cands = _candidates()
    high = {q for q, c in cands.items() if c.confidence == HIGH}
    assert high == {
        "com.example.DeadClass",
        "com.example.DeadClass.neverCalled",
        "com.example.ArrayList",  # unused import
        "com.example.Service.deadHelper",
        "com.example.Service.unusedPublic",
    }


def test_java_alive_not_reported():
    quals = set(_candidates())
    for q in (
        "com.example.App",  # contains main(): entry point
        "com.example.App.main",  # entry point
        "com.example.Service",
        "com.example.Service.<init>",  # reached via new Service()
        "com.example.Service.serve",  # called from App.main
        "com.example.Service.helper",  # called from serve()
        "com.example.Service.counter",  # read/written in serve()
    ):
        assert q not in quals, q


def test_java_reflection_downgraded_to_medium():
    # reflectiveOp is only reachable through the string literal in
    # Service.class.getMethod("reflectiveOp"): the scanner cannot see this
    # link statically (Java reflection), so it is downgraded to medium
    # instead of being reported high.
    cands = _candidates()
    assert cands["com.example.Service.reflectiveOp"].confidence == MEDIUM


def test_java_public_api_evidence_note():
    cands = _candidates()
    for q in ("com.example.Service.unusedPublic", "com.example.DeadClass"):
        c = cands[q]
        assert c.confidence == HIGH
        assert any("public API" in e for e in c.evidence), q
    # ... but the private dead method gets no such note.
    dead = cands["com.example.Service.deadHelper"]
    assert not any("public API" in e for e in dead.evidence)


def test_java_unused_import_evidence():
    cands = _candidates()
    assert "imported but never used" in cands["com.example.ArrayList"].evidence


def test_java_static_import_used_as_bare_call():
    # `import static java.lang.Math.max;` + `max(counter, 1)`: the bare call
    # must resolve through the import binding, not be flagged.
    quals = set(_candidates())
    assert "com.example.max" not in quals


def test_java_override_never_reported():
    # @Override methods are wired to their supertype: alive by construction,
    # even when nothing in the scanned codebase calls them.
    quals = set(_candidates())
    assert "com.example.Service.greet" not in quals
    assert "com.example.Greeter" not in quals
    assert "com.example.Greeter.greet" not in quals


def test_java_lang_filter():
    auto = {c.symbol.qualname for c in run(FIXTURE)}
    java = {c.symbol.qualname for c in run(FIXTURE, lang="java")}
    python = list(run(FIXTURE, lang="python"))
    assert auto == java
    assert len(auto) > 0
    assert python == []
    with pytest.raises(ValueError):
        run(FIXTURE, lang="kotlin")


def test_java_missing_dependency_error():
    from deadcode_hunter import scanner_java

    with patch.dict(sys.modules, {"tree_sitter": None, "tree_sitter_java": None}):
        scanner_java._PARSER = None
        try:
            with pytest.raises(RuntimeError, match="pip install deadcode-hunter\\[java\\]"):
                scanner_java.scan_java_directory(FIXTURE)
        finally:
            scanner_java._PARSER = None


# ---------------------------------------------------------------------------
# deletion (prove support)
# ---------------------------------------------------------------------------


@pytest.fixture()
def java_copy(tmp_path):
    dest = tmp_path / "proj"
    shutil.copytree(FIXTURE, dest)
    return dest


def _symbol(java_copy, qualname):
    cands = _candidates(java_copy)
    return cands[qualname].symbol


def test_delete_java_method(java_copy):
    target = java_copy / "com" / "example" / "Service.java"
    ok, reason = delete_java_symbol(target, _symbol(java_copy, "com.example.Service.deadHelper"))
    assert ok, reason
    text = target.read_text()
    assert "deadHelper" not in text
    assert "public void serve()" in text  # siblings untouched


def test_delete_java_import(java_copy):
    target = java_copy / "com" / "example" / "Service.java"
    ok, reason = delete_java_symbol(target, _symbol(java_copy, "com.example.ArrayList"))
    assert ok, reason
    assert "import java.util.ArrayList;" not in target.read_text()


def test_delete_java_class(java_copy):
    target = java_copy / "com" / "example" / "DeadClass.java"
    ok, reason = delete_java_symbol(target, _symbol(java_copy, "com.example.DeadClass"))
    assert ok, reason
    text = target.read_text()
    assert "class DeadClass" not in text
    assert "package com.example;" in text


def test_delete_java_field(java_copy):
    target = java_copy / "com" / "example" / "Service.java"
    # counter is alive (used in serve()), but deletion mechanics don't care:
    # fetch the symbol straight from the scan instead of the candidate list.
    from deadcode_hunter.scanner_java import scan_java_directory

    files = scan_java_directory(java_copy)
    sym = next(
        s
        for fi in files.values()
        for s in fi.symbols
        if s.qualname == "com.example.Service.counter"
    )
    ok, reason = delete_java_symbol(target, sym)
    assert ok, reason
    assert "private int counter" not in target.read_text()


def test_delete_java_multi_declarator_refused(tmp_path):
    target = tmp_path / "M.java"
    target.write_text("class M {\n    int a = 1, b = 2;\n}\n")
    info = scan_java_file(tmp_path, target, [0])
    sym = next(s for s in info.symbols if s.name == "a")
    ok, reason = delete_java_symbol(target, sym)
    assert not ok
    assert "other fields" in reason


def test_delete_java_oneliner_refused(tmp_path):
    # `class O { void m() {} }`: the method shares its line with the class;
    # deleting by lines would remove the class too.
    target = tmp_path / "O.java"
    target.write_text("class O { void m() {} }\n")
    info = scan_java_file(tmp_path, target, [0])
    sym = next(s for s in info.symbols if s.name == "m")
    ok, reason = delete_java_symbol(target, sym)
    assert not ok
    assert "shares lines" in reason


def _git(cmd, cwd):
    subprocess.run(["git"] + cmd, cwd=cwd, check=True, capture_output=True)


def test_prove_java_smoke(tmp_path):
    # End-to-end prove on a Java tree: deletion happens in a worktree and the
    # (stub) test command decides the outcome.
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo / "src")
    _git(["init"], repo)
    _git(["config", "user.email", "t@t.t"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "init"], repo)
    report = run_prove(
        repo / "src", min_confidence="high", test_cmd="true", lang="java", progress=False
    )
    counts = report.counts()
    assert counts[PROVEN] == 5
    # the worktree must be cleaned up and the working tree untouched
    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo, capture_output=True, text=True
    )
    assert status.stdout.strip() == ""
    wt = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True
    )
    assert len(wt.stdout.strip().splitlines()) == 1
