"""M8 baseline mode: track only *new* dead code.

Workflow:
    deadcode-hunter scan src --save-baseline baseline.json   # once, commit it
    deadcode-hunter scan src --baseline baseline.json --fail-on-new  # in CI

A baseline is a JSON file with a version marker and the identity of every
candidate at save time (file + qualname + kind + confidence, sorted for
stability). With ``--baseline``, only candidates absent from the baseline
are reported. ``--fail-on-new`` exits 1 when the (filtered) candidate list
is non-empty, so it also works without a baseline file as a plain
"no dead code" gate.
"""

import json
from pathlib import Path

from .models import Candidate

BASELINE_VERSION = 1


def _identity(candidate: Candidate) -> dict[str, str]:
    symbol = candidate.symbol
    return {
        "file": symbol.file,
        "symbol": symbol.qualname,
        "kind": symbol.kind,
        "confidence": candidate.confidence,
    }


def _sort_key(entry: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        entry["file"],
        entry["symbol"],
        entry["kind"],
        entry["confidence"],
    )


def save_baseline(candidates: list[Candidate], path: str | Path) -> int:
    """Write candidates to a baseline file. Returns the number saved."""
    payload = {
        "version": BASELINE_VERSION,
        "candidates": sorted(
            (_identity(c) for c in candidates), key=_sort_key
        ),
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return len(payload["candidates"])


def load_baseline(path: str | Path) -> set[tuple[str, str, str, str]]:
    """Load a baseline file, returning the set of candidate identities.

    Raises RuntimeError with a clear message when the file is missing,
    unreadable, or not a valid baseline.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read baseline file '{path}': {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid baseline file '{path}': not valid JSON ({exc})")
    if (
        not isinstance(data, dict)
        or data.get("version") != BASELINE_VERSION
        or not isinstance(data.get("candidates"), list)
    ):
        raise RuntimeError(
            f"invalid baseline file '{path}': expected "
            f'{{"version": {BASELINE_VERSION}, "candidates": [...]}}'
        )
    identities = set()
    for entry in data["candidates"]:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"invalid baseline file '{path}': candidate entries must be objects"
            )
        try:
            identities.add(
                (entry["file"], entry["symbol"], entry["kind"], entry["confidence"])
            )
        except KeyError as exc:
            raise RuntimeError(
                f"invalid baseline file '{path}': entry missing key {exc}"
            )
    return identities


def only_new(
    candidates: list[Candidate], baseline: set[tuple[str, str, str, str]]
) -> list[Candidate]:
    """Keep only candidates whose identity is absent from the baseline."""
    return [
        c
        for c in candidates
        if (
            c.symbol.file,
            c.symbol.qualname,
            c.symbol.kind,
            c.confidence,
        )
        not in baseline
    ]
