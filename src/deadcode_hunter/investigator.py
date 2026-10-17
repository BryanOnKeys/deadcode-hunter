"""Investigator: assigns confidence levels and builds evidence chains.

Confidence levels:
- high:   safe to delete (no references, no excuses)
- medium: needs a human glance (unresolved attribute refs, decorators, ...)
- low:    suspected alive (dynamic constructs like getattr/eval in scope)
"""

from .models import HIGH, LOW, MEDIUM, Candidate, FileInfo, Symbol


def _in_dynamic_scope(symbol: Symbol, file_info: FileInfo) -> bool:
    """True if the symbol's own body (or an enclosing scope) uses dynamic constructs."""
    for dynamic_scope in file_info.dynamic_scopes:
        if (
            symbol.scope == dynamic_scope
            or symbol.scope.startswith(dynamic_scope + ".")
            or symbol.qualname == dynamic_scope
        ):
            return True
    return False


def investigate(
    files: dict[str, FileInfo],
    strong: dict[int, int],
    weak: dict[int, int],
) -> list[Candidate]:
    """Build the candidate list with confidence levels and evidence."""
    candidates: list[Candidate] = []
    for file_info in files.values():
        for symbol in file_info.symbols:
            if (
                symbol.is_dunder
                or symbol.is_abstract
                or symbol.exported
                or symbol.in_main_block
                or symbol.is_entry_point
            ):
                continue  # alive by construction
            strong_refs = strong.get(symbol.sid, 0)
            if strong_refs > 0:
                continue  # proven used
            weak_refs = weak.get(symbol.sid, 0)

            evidence = [f"defined at {symbol.file}:{symbol.lineno}"]
            if symbol.kind == "import":
                evidence.append("imported but never used")
            elif weak_refs == 0:
                evidence.append("no references found in the scanned codebase")
            if weak_refs:
                evidence.append(
                    f"{weak_refs} weak reference(s): '{symbol.name}' appears in "
                    "attribute access that could not be resolved"
                )
            if symbol.decorators:
                evidence.append(
                    "decorated with " + ", ".join(f"@{d}" for d in symbol.decorators)
                )
            if symbol.in_test_file:
                evidence.append("defined in a test file")
            if _in_dynamic_scope(symbol, file_info):
                confidence = LOW
                evidence.append(
                    "scope uses dynamic constructs (getattr/eval/exec/__import__): "
                    "treated as possibly alive"
                )
            elif weak_refs > 0 or symbol.decorators:
                confidence = MEDIUM
            else:
                confidence = HIGH
            candidates.append(Candidate(symbol, confidence, strong_refs, weak_refs, evidence))
    candidates.sort(key=lambda c: (c.symbol.file, c.symbol.lineno))
    return candidates
