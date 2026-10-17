"""Core data models shared by the scan -> resolve -> investigate pipeline."""

from dataclasses import dataclass, field

# Confidence levels, ordered low -> high.
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

CONFIDENCE_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}

# Symbol kinds.
FUNCTION = "function"
METHOD = "method"
CLASS = "class"
IMPORT = "import"
VARIABLE = "variable"


@dataclass
class ImportBinding:
    """How a local name is bound by an import statement."""

    module: str  # absolute dotted module, e.g. "pkg.mod"
    symbol: str | None  # symbol inside module for `from` imports; None for plain module imports
    lineno: int


@dataclass
class Symbol:
    """A single definition found in source code."""

    sid: int
    name: str
    kind: str  # function | method | class | import | variable
    qualname: str  # e.g. "pkg.mod.Worker.run"
    scope: str  # qualname of the enclosing scope
    file: str  # path relative to the scan root, posix style
    lineno: int
    end_lineno: int
    decorators: list[str] = field(default_factory=list)
    binding: ImportBinding | None = None  # set for kind == "import"
    is_dunder: bool = False
    is_abstract: bool = False
    in_main_block: bool = False
    in_test_file: bool = False
    exported: bool = False  # listed in __all__
    is_entry_point: bool = False  # test functions, fixtures, ...


@dataclass
class NameRef:
    """A read of a bare name (ast.Name with Load/Del context)."""

    name: str
    scope: str  # qualname of the scope where it appears
    lineno: int
    is_store: bool = False  # True for `x = ...` inside a function body


@dataclass
class AttrRef:
    """A read of an attribute (ast.Attribute with Load context)."""

    attr: str
    base: str | None  # simple name when the base is a bare Name, else None
    dotted: str | None  # full dotted path when statically known, e.g. "pkg.mod.api"
    call_base: str | None  # class name when the base is a `ClassName(...)` call
    scope: str
    lineno: int


@dataclass
class FileInfo:
    """Everything extracted from one Python file."""

    path: str  # relative posix path
    module: str  # dotted module name, "" for unknown
    is_init: bool
    is_test: bool
    symbols: list[Symbol] = field(default_factory=list)
    name_refs: list[NameRef] = field(default_factory=list)
    attr_refs: list[AttrRef] = field(default_factory=list)
    module_imports: dict[str, ImportBinding] = field(default_factory=dict)
    star_imports: list[str] = field(default_factory=list)
    all_exports: set[str] = field(default_factory=set)
    dynamic_names: set[str] = field(default_factory=set)  # getattr(x, "literal") targets
    dynamic_scopes: set[str] = field(default_factory=set)  # scopes using dynamic constructs
    opaque_bases: set[tuple[str, str]] = field(default_factory=set)  # (scope, name) of params
    # annotated with external types (e.g. ast.*): attribute reads on them
    # can never reference our own symbols, so they create no weak refs
    scope_kinds: dict[str, str] = field(default_factory=dict)  # qualname -> module|class|function
    var_types: dict[tuple[str, str], str] = field(default_factory=dict)  # (scope, var) -> class name


@dataclass
class Candidate:
    """A symbol that looks dead, with evidence and a confidence level."""

    symbol: Symbol
    confidence: str  # high | medium | low
    strong_refs: int
    weak_refs: int
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        s = self.symbol
        return {
            "file": s.file,
            "symbol": s.qualname,
            "name": s.name,
            "kind": s.kind,
            "confidence": self.confidence,
            "lineno": s.lineno,
            "strong_refs": self.strong_refs,
            "weak_refs": self.weak_refs,
            "in_test_file": s.in_test_file,
            "evidence": self.evidence,
        }

    @property
    def evidence_summary(self) -> str:
        return "; ".join(self.evidence[1:3]) if len(self.evidence) > 1 else ""
