"""M11: find declared-but-unused Python dependencies.

``deadcode-hunter deps`` reads ``requirements.txt`` and/or
``pyproject.toml [project] dependencies``, then checks which declared
packages are actually imported anywhere in the codebase (reusing the
Python scanner's import extraction).

Import name -> distribution name mapping is heuristic: a built-in table
covers the well-known mismatches (``yaml`` -> ``PyYAML``,
``PIL`` -> ``pillow``, ``sklearn`` -> ``scikit-learn``, ...); everything
else is compared after normalizing case and ``_``/``-``/``.``.

Honesty rules:
- a dependency whose import name is found anywhere -> used;
- a dependency with no matching import -> unused;
- when the project uses dynamic imports (``__import__``, ``importlib``,
  ``eval``...), every "unused" verdict is suspect -> those go into a
  separate ``undetermined`` list instead of being declared dead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .scanner import scan_directory

# Well-known import-name -> distribution-name mismatches.
_IMPORT_TO_DIST = {
    "yaml": "pyyaml",
    "PIL": "pillow",
    "pillow": "pillow",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "attr": "attrs",
    "attrs": "attrs",
    "gi": "pygobject",
    "wx": "wxpython",
    "serial": "pyserial",
    "Crypto": "pycryptodome",
    "Cryptodome": "pycryptodome",
    "OpenSSL": "pyopenssl",
    "jwt": "pyjwt",
    "dotenv": "python-dotenv",
    "magic": "python-magic",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "telegram": "python-telegram-bot",
    "jose": "python-jose",
    "multipart": "python-multipart",
    "socks": "pysocks",
    "usb": "pyusb",
    "usb1": "libusb1",
    "lxml": "lxml",
    "stem": "stem",
    "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary",
    "nacl": "pynacl",
    "zmq": "pyzmq",
    "Bio": "biopython",
    "py7zr": "py7zr",
    "rarfile": "rarfile",
    "rtoml": "rtoml",
    "tomli": "tomli",
    "tomli_w": "tomli-w",
    "ujson": "ujson",
    "orjson": "orjson",
    "msgpack": "msgpack",
    "bson": "pymongo",
    "pymongo": "pymongo",
    "gridfs": "pymongo",
    "redis": "redis",
    "rq": "rq",
    "celery": "celery",
    "kombu": "kombu",
    "billiard": "billiard",
    "vine": "vine",
    "amqp": "amqp",
    "click": "click",
    "typer": "typer",
    "rich": "rich",
    "tqdm": "tqdm",
    "pytest": "pytest",
    "hypothesis": "hypothesis",
    "mock": "mock",
    "faker": "faker",
    "factory": "factory-boy",
    "freezegun": "freezegun",
    "responses": "responses",
    "requests": "requests",
    "urllib3": "urllib3",
    "httpx": "httpx",
    "aiohttp": "aiohttp",
    "flask": "flask",
    "django": "django",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "gunicorn": "gunicorn",
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "pydantic": "pydantic",
    "marshmallow": "marshmallow",
    "numpy": "numpy",
    "scipy": "scipy",
    "pandas": "pandas",
    "matplotlib": "matplotlib",
    "seaborn": "seaborn",
    "plotly": "plotly",
    "bokeh": "bokeh",
    "sklearn_pandas": "sklearn-pandas",
    "torch": "torch",
    "torchvision": "torchvision",
    "tensorflow": "tensorflow",
    "keras": "keras",
    "transformers": "transformers",
    "datasets": "datasets",
    "tokenizers": "tokenizers",
    "accelerate": "accelerate",
    "peft": "peft",
    "openai": "openai",
    "anthropic": "anthropic",
    "tiktoken": "tiktoken",
    "langchain": "langchain",
    "llama_index": "llama-index",
    "chromadb": "chromadb",
    "faiss": "faiss-cpu",
    "sentence_transformers": "sentence-transformers",
}


def _normalize_dist(name: str) -> str:
    """Normalize a distribution name: lower, _/-/., strip extras/markers."""
    name = name.strip()
    # strip environment markers
    name = name.split(";")[0].strip()
    # strip extras: requests[socks] -> requests
    name = name.split("[")[0].strip()
    # strip version specifiers
    name = re.split(r"[=<>!~]+", name, maxsplit=1)[0].strip()
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_import(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_for_import(import_name: str) -> str:
    """Best-guess distribution name for a top-level import name."""
    if import_name in _IMPORT_TO_DIST:
        return _normalize_dist(_IMPORT_TO_DIST[import_name])
    return _normalize_import(import_name)


@dataclass
class DeclaredDep:
    """A dependency declared in requirements.txt / pyproject.toml."""

    name: str  # as declared, e.g. "requests"
    raw: str  # the full requirement line
    source: str  # "requirements.txt" or "pyproject.toml"


@dataclass
class DepsReport:
    used: list[DeclaredDep] = field(default_factory=list)
    unused: list[DeclaredDep] = field(default_factory=list)
    undetermined: list[DeclaredDep] = field(default_factory=list)
    skipped_lines: list[str] = field(default_factory=list)
    dynamic_imports_detected: bool = False

    def to_dict(self, path: str) -> dict:
        def conv(ds: list[DeclaredDep]) -> list[dict]:
            return [
                {"name": d.name, "raw": d.raw, "source": d.source} for d in ds
            ]

        return {
            "path": path,
            "used": conv(self.used),
            "unused": conv(self.unused),
            "undetermined": conv(self.undetermined),
            "skipped_lines": self.skipped_lines,
            "dynamic_imports_detected": self.dynamic_imports_detected,
        }


def parse_requirements(path: Path) -> tuple[list[DeclaredDep], list[str]]:
    """Parse requirements.txt. Returns (deps, skipped_lines)."""
    deps: list[DeclaredDep] = []
    skipped: list[str] = []
    for lineno, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            # -r nested.txt, -e ., --index-url ... : not supported yet.
            skipped.append(f"line {lineno}: {raw_line.strip()} (not supported)")
            continue
        # strip inline comments (careful: URLs may contain #)
        if "://" not in line and "#" in line:
            line = line.split("#", 1)[0].strip()
        if "://" in line:
            skipped.append(f"line {lineno}: {raw_line.strip()} (URL requirement not supported)")
            continue
        # _normalize_dist on a bare name returns it lowercased; recover the
        # original spelling for display from the raw line.
        display = re.split(r"[=<>!~;\[]+", line.strip(), maxsplit=1)[0].strip()
        if not display:
            skipped.append(f"line {lineno}: unparseable")
            continue
        deps.append(
            DeclaredDep(name=display, raw=raw_line.strip(), source="requirements.txt")
        )
    return deps, skipped


def parse_pyproject_dependencies(path: Path) -> list[DeclaredDep]:
    """Parse [project] dependencies from pyproject.toml."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, Exception):
        return []
    raw_deps = data.get("project", {}).get("dependencies", []) or []
    deps: list[DeclaredDep] = []
    for raw in raw_deps:
        display = re.split(r"[=<>!~;\[]+", str(raw).strip(), maxsplit=1)[0].strip()
        if display:
            deps.append(
                DeclaredDep(name=display, raw=str(raw).strip(), source="pyproject.toml")
            )
    return deps


def _collect_imports(root: Path) -> tuple[set[str], bool]:
    """Top-level imported package names + whether dynamic imports are used."""
    files = scan_directory(root, [0])
    imported: set[str] = set()
    dynamic = False
    for info in files.values():
        for binding in info.module_imports.values():
            top = binding.module.split(".")[0]
            imported.add(top)
        if info.dynamic_scopes:
            dynamic = True
    return imported, dynamic


def run_deps(root: str | Path) -> DepsReport:
    """Find declared-but-unused dependencies under root."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise RuntimeError(f"not a directory: {root}")
    declared: list[DeclaredDep] = []
    skipped: list[str] = []
    req = root_path / "requirements.txt"
    if req.is_file():
        deps, skipped = parse_requirements(req)
        declared.extend(deps)
    ppt = root_path / "pyproject.toml"
    if ppt.is_file():
        declared.extend(parse_pyproject_dependencies(ppt))
    if not declared and not skipped:
        raise RuntimeError(
            "no dependencies found: expected requirements.txt or "
            "[project] dependencies in pyproject.toml"
        )
    imported, dynamic = _collect_imports(root_path)
    # Map every imported top-level name to its guessed distribution name.
    provided = {_dist_for_import(name) for name in imported}
    report = DepsReport(skipped_lines=skipped, dynamic_imports_detected=dynamic)
    for dep in declared:
        if _normalize_dist(dep.name) in provided:
            report.used.append(dep)
        elif dynamic:
            report.undetermined.append(dep)
        else:
            report.unused.append(dep)
    return report
