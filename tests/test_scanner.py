"""Tests against the fixture sample repo covering every M1 rule."""

from pathlib import Path

import pytest

from deadcode_hunter.pipeline import run as scan

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(scope="module")
def results():
    candidates = scan(FIXTURE)
    by_symbol = {c.symbol.qualname: c for c in candidates}
    return candidates, by_symbol


EXPECTED_HIGH = {
    "alive._private_unused",
    "dead.os",
    "dead.system",
    "dead.DEAD_CONST",
    "dead.dead_func",
    "dead.DeadClass",
    "dead.DeadClass.dead_method",
    "decorated.Page",
    "dunder_abc.Base",
    "dunder_abc.Base.concrete",
    "main_block._private_main",
    "pkg.mod.internal_dead",
    "pkg.mod.unused_top",
    "sibling.use_it",
    "star.STAR_VAR",
    "tests.test_sample.unused_helper",
    "tricky.outer",
    "tricky.outer2",
    "tricky.outer3",
    "tricky.outer3.inner3",
    "worker.Worker.dead_method",
}

EXPECTED_MEDIUM = {
    "decorated.index",
    "decorated.Page.title",
}

EXPECTED_LOW = {
    "dynamic.fetch",
    "dynamic.load",
}


def test_high_confidence_dead_code(results):
    _, by_symbol = results
    high = {s for s, c in by_symbol.items() if c.confidence == "high"}
    assert high == EXPECTED_HIGH


def test_medium_confidence_decorated(results):
    _, by_symbol = results
    medium = {s for s, c in by_symbol.items() if c.confidence == "medium"}
    assert medium == EXPECTED_MEDIUM


def test_low_confidence_dynamic(results):
    _, by_symbol = results
    low = {s for s, c in by_symbol.items() if c.confidence == "low"}
    assert low == EXPECTED_LOW


@pytest.mark.parametrize(
    "symbol",
    [
        # __all__ exports
        "alive.exported_thing",
        "alive.PUBLIC_CONST",
        # cross-file name references
        "alive.used_alive",
        # __init__ re-export + dotted usage
        "pkg.public_api",
        "pkg.mod.public_api",
        # self / var_types / call_base method resolution
        "worker.Worker",
        "worker.Worker.live_method",
        "worker.Worker._helper",
        "pkg.mod.Greeter",
        "pkg.mod.Greeter.greet",
        "pkg.mod.Greeter._format",
        # imports that are actually used
        "consumer.used_alive",
        "consumer.Worker",
        "consumer.mod_alias",
        "consumer.Greeter",
        "consumer.worker",
        # recursion and closures
        "tricky.recursive",
        "tricky.outer.inner",
        "tricky.outer2.inner2",
        # __main__ block
        "main_block.main",
        "main_block._runner",
        # dunder / abstract never reported
        "dunder_abc.Base.must_implement",
        "dunder_abc.Base.__str__",
        # test file handling: fixtures, test_ functions, called helpers
        "tests.test_sample.db",
        "tests.test_sample.test_addition",
        "tests.test_sample.helper",
        "tests.test_sample.pytest",
        # decorator machinery
        "decorated.route",
        "decorated.route.deco",
        "decorated.ROUTES",
    ],
)
def test_alive_symbols_not_reported(results, symbol):
    _, by_symbol = results
    assert symbol not in by_symbol, f"{symbol} should be alive, got {by_symbol[symbol].evidence}"


def test_evidence_chain_present(results):
    _, by_symbol = results
    candidate = by_symbol["dead.dead_func"]
    assert candidate.evidence  # non-empty
    assert any("defined at" in e for e in candidate.evidence)
