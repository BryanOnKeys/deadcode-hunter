"""M9 project-local configuration.

Reads ``[tool.deadcode-hunter]`` from the *scan target's own* ``pyproject.toml``
(no upward recursion — we never want to accidentally pick up some parent
project's config):

```toml
[tool.deadcode-hunter]
exclude = ["tests/fixtures/**", "docs/**"]   # glob, matched against the
                                             # scan-root-relative file path
entry-points = ["mypackage.api.*", "mypackage.main"]  # glob, matched against
                                                      # the symbol qualname
min-confidence = "high"      # default for scan/prove/clean
test-cmd = "pytest -q"       # default test command for prove/clean
```

CLI flags always override config file values.
"""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ImportError:  # Python 3.10: `tomli` backport (declared in pyproject)
    import tomli as tomllib  # type: ignore[no-redef]

from .models import CONFIDENCE_ORDER

SECTION = ("tool", "deadcode-hunter")


def _section(data: dict) -> dict:
    """Walk SECTION through nested dicts; {} when anything is missing."""
    node: object = data
    for key in SECTION:
        if not isinstance(node, dict):
            return {}
        node = node.get(key, {})
    return node if isinstance(node, dict) else {}


@dataclass
class HunterConfig:
    """Effective configuration for one scan target."""

    exclude: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    min_confidence: str | None = None
    test_cmd: str | None = None


def _as_str_list(value: object, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RuntimeError(
            f"[tool.deadcode-hunter] {key} must be a list of strings"
        )
    return list(value)


def load_config(root: str | Path) -> HunterConfig:
    """Load ``[tool.deadcode-hunter]`` from ``<root>/pyproject.toml``.

    Returns an empty config when the file or the section is absent.
    Raises RuntimeError on malformed TOML or invalid values.
    """
    path = Path(root) / "pyproject.toml"
    if not path.is_file():
        return HunterConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:  # type: ignore[attr-defined]
        raise RuntimeError(f"cannot parse {path}: {exc}")
    section = _section(data)
    cfg = HunterConfig()
    if "exclude" in section:
        cfg.exclude = _as_str_list(section["exclude"], "exclude")
    if "entry-points" in section:
        cfg.entry_points = _as_str_list(section["entry-points"], "entry-points")
    if "min-confidence" in section:
        value = section["min-confidence"]
        if not isinstance(value, str) or value.lower() not in CONFIDENCE_ORDER:
            raise RuntimeError(
                "[tool.deadcode-hunter] min-confidence must be high, medium or low"
            )
        cfg.min_confidence = value.lower()
    if "test-cmd" in section:
        value = section["test-cmd"]
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                "[tool.deadcode-hunter] test-cmd must be a non-empty string"
            )
        cfg.test_cmd = value
    return cfg


def matches_any(value: str, patterns: list[str]) -> bool:
    """Glob-match `value` against patterns (``*`` crosses ``/``)."""
    return any(fnmatch.fnmatchcase(value, pat) for pat in patterns)
