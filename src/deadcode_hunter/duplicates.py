"""M12: duplicate-code detection for Python.

``deadcode-hunter duplicates`` finds copy-pasted code blocks: each line is
normalized at the token level (identifiers -> ``ID``, strings/numbers ->
``LIT``, keywords and operators kept), then a sliding window of
``--min-lines`` lines is hashed. Windows with equal hashes are extended
bidirectionally into maximal blocks; blocks are grouped, and groups below
``--min-tokens`` total tokens are dropped.

Only Python is supported in v1 (TypeScript/Java are roadmap). The
tokenizer is stdlib ``tokenize``; lines that cannot be tokenized (or are
blank/comment-only) break windows rather than producing false matches.
"""

from __future__ import annotations

import hashlib
import io
import keyword
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from .scanner import iter_python_files

_SKIP_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
)


def _normalized_lines(path: Path) -> list[str | None]:
    """Token-normalized source lines; None for blank/unparseable lines."""
    try:
        src = path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = src.splitlines()
    per_line: dict[int, list[str]] = {}
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in toks:
            if tok.type in _SKIP_TOKENS:
                continue
            if tok.type == tokenize.NAME and not keyword.iskeyword(tok.string):
                rep = "ID"
            elif tok.type in (tokenize.NUMBER, tokenize.STRING):
                rep = "LIT"
            else:
                rep = tok.string
            per_line.setdefault(tok.start[0], []).append(rep)
    except (tokenize.TokenError, SyntaxError, IndentationError):
        # Unparseable file: no windows at all (never a false positive).
        return [None] * len(lines)
    return [
        " ".join(per_line.get(i + 1, [])) or None for i in range(len(lines))
    ]


@dataclass
class DupBlock:
    file: str  # path relative to the scan root, posix style
    start: int  # 1-based, inclusive
    end: int  # 1-based, inclusive

    @property
    def line_count(self) -> int:
        return self.end - self.start + 1

    def to_dict(self) -> dict:
        return {"file": self.file, "start": self.start, "end": self.end}


@dataclass
class DupGroup:
    blocks: list[DupBlock] = field(default_factory=list)
    tokens: int = 0

    @property
    def lines(self) -> int:
        return self.blocks[0].line_count if self.blocks else 0

    def to_dict(self) -> dict:
        return {
            "lines": self.lines,
            "tokens": self.tokens,
            "occurrences": len(self.blocks),
            "blocks": [b.to_dict() for b in self.blocks],
        }


def _window_hash(norm: list[str | None], start: int, size: int) -> str | None:
    window = norm[start : start + size]
    if any(line is None for line in window):
        return None
    return hashlib.sha256("\n".join(window).encode()).hexdigest()  # type: ignore[arg-type]


def _extend(
    files_norm: dict[str, list[str | None]],
    a: tuple[str, int],
    b: tuple[str, int],
) -> tuple[DupBlock, DupBlock]:
    """Bidirectionally extend two matching windows into maximal blocks."""
    fa, ia = a
    fb, ib = b
    na, nb = files_norm[fa], files_norm[fb]
    while (
        ia > 0
        and ib > 0
        and na[ia - 1] is not None
        and na[ia - 1] == nb[ib - 1]
    ):
        ia -= 1
        ib -= 1
    ja, jb = ia, ib
    while (
        ja + 1 < len(na)
        and jb + 1 < len(nb)
        and na[ja + 1] is not None
        and na[ja + 1] == nb[jb + 1]
    ):
        ja += 1
        jb += 1
    # ia/ib are 0-based starts; ja/jb are 0-based ends.
    return (
        DupBlock(fa, ia + 1, ja + 1),
        DupBlock(fb, ib + 1, jb + 1),
    )


def _token_count(norm: list[str | None], start: int, end: int) -> int:
    """Total normalized tokens in 1-based [start, end]."""
    total = 0
    for line in norm[start - 1 : end]:
        if line:
            total += len(line.split())
    return total


def find_duplicates(
    root: str | Path, min_lines: int = 6, min_tokens: int = 50
) -> list[DupGroup]:
    """Find duplicated code blocks under root (Python only)."""
    root_path = Path(root)
    if root_path.is_file():
        paths = [root_path]
        root_path = root_path.parent
    else:
        paths = iter_python_files(root_path)
    files_norm: dict[str, list[str | None]] = {}
    for path in paths:
        rel = path.relative_to(root_path).as_posix()
        norm = _normalized_lines(path)
        if norm:
            files_norm[rel] = norm

    # Index every min_lines window by hash.
    by_hash: dict[str, list[tuple[str, int]]] = {}
    for rel, norm in files_norm.items():
        for i in range(len(norm) - min_lines + 1):
            h = _window_hash(norm, i, min_lines)
            if h is not None:
                by_hash.setdefault(h, []).append((rel, i))

    groups: list[DupGroup] = []
    seen: set[frozenset[tuple[str, int, int]]] = set()
    for occurrences in by_hash.values():
        if len(occurrences) < 2:
            continue
        if len(occurrences) > 500:
            # Degenerate hash (e.g. hundreds of identical trivial lines):
            # skip rather than burn quadratic time.
            continue
        anchor = occurrences[0]
        blocks: dict[tuple[str, int, int], DupBlock] = {}
        for other in occurrences[1:]:
            ba, bb = _extend(files_norm, anchor, other)
            for b in (ba, bb):
                blocks[(b.file, b.start, b.end)] = b
        if len(blocks) < 2:
            continue
        key = frozenset(blocks.keys())
        if key in seen:
            continue
        seen.add(key)
        ordered = sorted(blocks.values(), key=lambda b: (b.file, b.start))
        tokens = _token_count(
            files_norm[ordered[0].file], ordered[0].start, ordered[0].end
        )
        if tokens < min_tokens:
            continue
        groups.append(DupGroup(blocks=ordered, tokens=tokens))

    # Largest groups first.
    groups.sort(key=lambda g: (-g.lines, -len(g.blocks)))
    return groups
