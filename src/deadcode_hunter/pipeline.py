"""End-to-end analysis pipeline: scan -> resolve -> investigate."""

from pathlib import Path

from .investigator import investigate
from .models import Candidate
from .resolver import resolve_references
from .scanner import scan_directory as _scan_python


def run(root: str | Path, lang: str = "auto") -> list[Candidate]:
    """Run the full analysis on a directory (or a single file).

    lang: 'auto' (default), 'python' or 'java'. With 'auto', .py files are
    scanned with the Python engine and .java files with the Java engine
    (which needs ``pip install deadcode-hunter[java]``).
    """
    normalized = (lang or "auto").lower()
    if normalized not in ("auto", "python", "java"):
        raise ValueError("--lang must be 'auto', 'python' or 'java'")
    sid_counter = [0]
    files = {}
    if normalized in ("auto", "python"):
        files.update(_scan_python(root, sid_counter))
    if normalized in ("auto", "java"):
        from .scanner_java import scan_java_directory

        files.update(scan_java_directory(root, sid_counter))
    strong, weak = resolve_references(files)
    return investigate(files, strong, weak)
