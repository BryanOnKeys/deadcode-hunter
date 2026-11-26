"""Regression tests for combining results from the language scanners."""

from deadcode_hunter.pipeline import run


def test_auto_scan_keeps_symbol_ids_unique_across_languages(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def called():\n    return 1\n\ncalled()\n\ndef unused():\n    return 2\n"
    )
    (tmp_path / "Example.java").write_text("class Example { void unused() {} }\n")
    (tmp_path / "sample.ts").write_text("function unused() { return 3; }\n")

    candidates = run(tmp_path)
    ids = [candidate.symbol.sid for candidate in candidates]
    paths = {candidate.symbol.file for candidate in candidates}

    assert paths == {"sample.py", "Example.java", "sample.ts"}
    assert len(ids) == len(set(ids))
    assert "sample.called" not in {candidate.symbol.qualname for candidate in candidates}
