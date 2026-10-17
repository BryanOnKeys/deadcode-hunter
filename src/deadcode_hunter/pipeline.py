"""End-to-end analysis pipeline: scan -> resolve -> investigate."""

from pathlib import Path

from .config import HunterConfig, matches_any
from .investigator import investigate
from .models import Candidate
from .resolver import resolve_references
from .scanner import scan_directory as _scan_python


def run(
    root: str | Path, lang: str = "auto", config: HunterConfig | None = None
) -> list[Candidate]:
    """Run the full analysis on a directory (or a single file).

    lang: 'auto' (default), 'python', 'java' or 'typescript'. With 'auto',
    .py files are scanned with the Python engine, .java files with the Java
    engine (needs ``pip install deadcode-hunter[java]``) and .ts/.tsx/.js/
    .jsx files with the TypeScript engine (needs
    ``pip install deadcode-hunter[typescript]``).

    config: optional HunterConfig — excluded files are dropped before
    resolving, and entry-point symbols are never reported.
    """
    normalized = (lang or "auto").lower()
    if normalized not in ("auto", "python", "java", "typescript"):
        raise ValueError("--lang must be 'auto', 'python', 'java' or 'typescript'")
    sid_counter = [0]
    files = {}
    if normalized in ("auto", "python"):
        files.update(_scan_python(root, sid_counter))
    if normalized in ("auto", "java"):
        from .scanner_java import scan_java_directory

        files.update(scan_java_directory(root, sid_counter))
    if normalized in ("auto", "typescript"):
        from .scanner_typescript import scan_typescript_directory

        files.update(scan_typescript_directory(root, sid_counter))
    if config is not None and config.exclude:
        files = {
            path: info
            for path, info in files.items()
            if not matches_any(path, config.exclude)
        }
    strong, weak = resolve_references(files)
    candidates = investigate(files, strong, weak)
    if config is not None and config.entry_points:
        candidates = [
            c
            for c in candidates
            if not matches_any(c.symbol.qualname, config.entry_points)
        ]
    return candidates
