"""Tests for the TypeScript/JavaScript scanner (tree-sitter based)."""

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from deadcode_hunter.models import HIGH, MEDIUM
from deadcode_hunter.pipeline import run
from deadcode_hunter.scanner_typescript import (
    delete_typescript_symbol,
    scan_typescript_file,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_ts"


def _candidates(path=FIXTURE, lang="typescript"):
    return {c.symbol.qualname: c for c in run(path, lang=lang)}


def test_typescript_high_set():
    cands = _candidates()
    high = {q for q, c in cands.items() if c.confidence == HIGH}
    assert high == {
        "app.unusedImport",  # unused import
        "app.Service.deadMethod",
        "app.deadTopLevel",
        "app.deadArrow",
        "app.DeadClass",
        "app.DeadClass.run",
        "app.unusedVar",
        "shapes.InternalDeadEnum",
        "util.internalDead",
    }


def test_typescript_alive_not_reported():
    quals = set(_candidates())
    for q in (
        "app.helper",  # imported and used
        "app.usesInternal",  # imported and used
        "app.path",  # `import * as path` used via path.join
        "app.Service",  # instantiated
        "app.Service.serve",  # called
        "app.Service.helper2",  # called via this.helper2()
        "app.main.s",  # `const s = new Service()` — s is read by s.serve()
        "app.usedArrow",
        "app.usedVar",
        "app.checkVars",  # called from main()
        "shapes.LiveEnum",  # exported
        "shapes.Circle",  # exported
        "shapes.Circle.area",  # member of an exported class
        "util.helper",  # exported
    ):
        assert q not in quals, q


def test_typescript_exported_never_reported():
    # Exported symbols are the module's public surface (like Python's
    # __all__): the tool cannot see external consumers, so they are never
    # reported — even when unused inside the codebase.
    quals = set(_candidates())
    assert "util.exportedConst" not in quals
    assert "shapes.useEnum" not in quals
    # `export { a }` specifier form marks the local name exported too.
    assert "app.main" not in quals  # `export function main` + `export default main`


def test_typescript_decorated_downgraded_to_medium():
    cands = _candidates()
    assert cands["app.Decorated"].confidence == MEDIUM
    # Members of a decorated class inherit the downgrade.
    assert cands["app.Decorated.x"].confidence == MEDIUM


def test_typescript_console_is_not_a_reference():
    # `console.log(...)` must not create noise references; in particular
    # nothing named `log` should be kept alive or reported because of it.
    cands = _candidates()
    assert not any(q.endswith(".log") for q in cands)


def test_typescript_lang_filter():
    # --lang python on a TS directory finds nothing (and vice versa).
    assert run(FIXTURE, lang="python") == []
    assert len(run(FIXTURE, lang="typescript")) > 0


def test_typescript_missing_dependency_error():
    from deadcode_hunter import scanner_typescript

    mods = {
        "tree_sitter": None,
        "tree_sitter_typescript": None,
        "tree_sitter_javascript": None,
    }
    with patch.dict(sys.modules, mods):
        scanner_typescript._PARSERS.clear()
        try:
            with pytest.raises(
                RuntimeError, match=r"pip install deadcode-hunter\[typescript\]"
            ):
                scanner_typescript.scan_typescript_directory(FIXTURE)
        finally:
            scanner_typescript._PARSERS.clear()


def test_typescript_js_and_tsx():
    # .js uses the javascript grammar, .tsx the tsx grammar (incl. JSX).
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.js").write_text(
            "function deadJs() { return 1; }\nfunction liveJs() { return 2; }\nliveJs();\n"
        )
        (root / "b.tsx").write_text(
            "import { useState } from 'react';\n"
            "function DeadComp() { return <div/>; }\n"
            "export function App() { const [x] = useState(0); return <span>{x}</span>; }\n"
        )
        cands = {c.symbol.qualname: c for c in run(root, lang="typescript")}
        high = {q for q, c in cands.items() if c.confidence == HIGH}
        assert "a.deadJs" in high
        assert "a.liveJs" not in cands
        assert "b.DeadComp" in high
        assert "b.App" not in cands  # exported


# ---------------------------------------------------------------------------
# deletion (prove support)
# ---------------------------------------------------------------------------


@pytest.fixture()
def ts_copy(tmp_path):
    src = FIXTURE / "app.ts"
    dst = tmp_path / "app.ts"
    shutil.copy(src, dst)
    return dst


def _symbol_by_qualname(path, qualname):
    from pathlib import Path as P

    info = scan_typescript_file(P(path).parent, P(path), [0])
    for sym in info.symbols:
        if sym.qualname == qualname:
            return sym
    raise AssertionError(f"no symbol {qualname}")


def test_typescript_delete_function(ts_copy):
    sym = _symbol_by_qualname(ts_copy, "app.deadTopLevel")
    ok, reason = delete_typescript_symbol(ts_copy, sym)
    assert ok, reason
    text = ts_copy.read_text()
    assert "deadTopLevel" not in text
    assert "function checkVars" in text  # untouched


def test_typescript_delete_import(ts_copy):
    sym = _symbol_by_qualname(ts_copy, "app.unusedImport")
    ok, reason = delete_typescript_symbol(ts_copy, sym)
    assert ok, reason
    text = ts_copy.read_text()
    assert "unusedImport" not in text
    assert "./util" in text  # the other import survives


def test_typescript_delete_class(ts_copy):
    sym = _symbol_by_qualname(ts_copy, "app.DeadClass")
    ok, reason = delete_typescript_symbol(ts_copy, sym)
    assert ok, reason
    text = ts_copy.read_text()
    assert "DeadClass" not in text
    # The file still parses cleanly after deletion.
    from deadcode_hunter.scanner_typescript import _parser_for, _tree_has_error

    tree = _parser_for(".ts").parse(text.encode())
    assert not _tree_has_error(tree.root_node)
