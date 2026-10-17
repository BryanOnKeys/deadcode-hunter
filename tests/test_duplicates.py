"""Tests for duplicate-code detection (`deadcode-hunter duplicates`)."""

from pathlib import Path

from deadcode_hunter.duplicates import find_duplicates

FIXTURE = Path(__file__).parent / "fixtures" / "sample_dup"


def _locations(groups):
    return [
        sorted((b.file, b.start, b.end) for b in g.blocks) for g in groups
    ]


def test_duplicates_cross_file_group():
    groups = find_duplicates(FIXTURE)
    locs = _locations(groups)
    assert [("a.py", 1, 11), ("b.py", 1, 11)] in locs


def test_duplicates_same_file_group():
    # c.py's two 7-line blocks (~27 tokens) need a lower token threshold.
    groups = find_duplicates(FIXTURE, min_tokens=20)
    locs = _locations(groups)
    assert [("c.py", 1, 7), ("c.py", 10, 16)] in locs


def test_duplicates_near_miss_not_reported():
    # d.py looks similar but normalizes differently (`while` vs `for`):
    # it must never appear in any group.
    for kwargs in ({}, {"min_tokens": 20}, {"min_lines": 4, "min_tokens": 10}):
        for group in find_duplicates(FIXTURE, **kwargs):
            assert not any(b.file == "d.py" for b in group.blocks)


def test_duplicates_min_lines_filter():
    # e.py's 4-line blocks are invisible at the default min-lines=6 ...
    default_locs = _locations(find_duplicates(FIXTURE))
    assert not any(("e.py", 1, 4) in loc for loc in default_locs)
    # ... but show up with --min-lines 4.
    locs = _locations(find_duplicates(FIXTURE, min_lines=4, min_tokens=10))
    assert [("e.py", 1, 4), ("e.py", 7, 10)] in locs


def test_duplicates_min_tokens_filter():
    # The 11-line cross-file group has 56 tokens: visible at 50, gone at 60.
    assert len(find_duplicates(FIXTURE, min_tokens=60)) == 0
    assert len(find_duplicates(FIXTURE, min_tokens=50)) == 1


def test_duplicates_single_file_target():
    groups = find_duplicates(FIXTURE / "a.py", min_tokens=1, min_lines=11)
    # Only one file scanned: no pair to form a group.
    assert groups == []
