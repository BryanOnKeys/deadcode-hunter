"""Tests for unused-dependency detection (`deadcode-hunter deps`)."""

import tempfile
from pathlib import Path

import pytest

from deadcode_hunter.deps import (
    _dist_for_import,
    _normalize_dist,
    parse_requirements,
    run_deps,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_deps"


def test_deps_unused_set():
    report = run_deps(FIXTURE)
    assert [d.name for d in report.unused] == ["some-unused-pkg"]
    used = {d.name for d in report.used}
    assert used == {"requests", "PyYAML", "numpy", "scikit-learn"}
    assert report.undetermined == []
    # `-r nested.txt` is recorded as skipped, not crashed on.
    assert any("-r nested.txt" in line for line in report.skipped_lines)


def test_deps_misaligned_names_resolve():
    # yaml -> PyYAML, sklearn -> scikit-learn via the mapping table.
    assert _dist_for_import("yaml") == "pyyaml"
    assert _dist_for_import("sklearn") == "scikit-learn"
    assert _dist_for_import("PIL") == "pillow"
    # Heuristic fallback: underscores/dashes/case normalized.
    assert _dist_for_import("my_package") == _normalize_dist("my-package")


def test_deps_pyproject_source(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2", "never-used-lib==1.0"]\n'
    )
    (tmp_path / "m.py").write_text("import requests\n")
    report = run_deps(tmp_path)
    assert [d.name for d in report.unused] == ["never-used-lib"]
    assert [d.name for d in report.used] == ["requests"]
    assert report.unused[0].source == "pyproject.toml"


def test_deps_dynamic_imports_go_undetermined(tmp_path):
    (tmp_path / "requirements.txt").write_text("maybe-dynamic==1.0\n")
    (tmp_path / "m.py").write_text('mod = __import__("maybe_dynamic")\n')
    report = run_deps(tmp_path)
    assert report.dynamic_imports_detected
    # Not declared dead: dynamic imports could load it at runtime.
    assert report.unused == []
    assert [d.name for d in report.undetermined] == ["maybe-dynamic"]


def test_deps_no_manifest_errors(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    with pytest.raises(RuntimeError, match="no dependencies found"):
        run_deps(tmp_path)


def test_deps_requirements_parsing():
    with tempfile.TemporaryDirectory() as tmp:
        req = Path(tmp) / "requirements.txt"
        req.write_text(
            "requests[socks]>=2.0  # with extras and comment\n"
            "Django==4.2; python_version >= '3.8'\n"
            "git+https://github.com/x/y.git\n"
            "\n"
        )
        deps, skipped = parse_requirements(req)
        assert [d.name for d in deps] == ["requests", "Django"]
        assert any("git+https" in s for s in skipped)
