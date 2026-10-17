"""TypeScript/JavaScript dead-code scanner built on tree-sitter.

Produces the same :class:`FileInfo` structures as the Python and Java
scanners, so the shared resolver/investigator pipeline works unchanged
across languages.

tree-sitter is an optional dependency::

    pip install deadcode-hunter[typescript]

Supported extensions: ``.ts`` (typescript grammar), ``.tsx`` (tsx grammar),
``.js`` / ``.jsx`` (javascript grammar).

Design notes (TypeScript -> shared-model mapping):

- The FileInfo ``module`` is the file's dotted path relative to the scan
  root (``src/util.ts`` -> ``src.util``; ``src/index.ts`` -> ``src``), so
  cross-file import resolution works like the other engines.
- Relative imports (``./util``, ``../lib``) are resolved to files on disk
  (trying ``.ts``/``.tsx``/``.js``/``.jsx`` and ``/index.*``); bare imports
  (``"react"``, ``"path"``) are treated as external packages.
- ``export``-ed symbols are the module's public surface and are never
  reported (like Python's ``__all__``); the README documents this choice.
- Decorated classes/functions are downgraded to medium, like the other
  engines. Interfaces and type aliases are contracts and are never
  reported; enums are real values and are analyzed.
- ``console``/``process``/``window``/``globalThis``/``document``/
  ``navigator`` member accesses are skipped: they are globals, not
  references to anything we could flag.
"""

from __future__ import annotations

import os
from pathlib import Path

from .models import (
    CLASS,
    FUNCTION,
    IMPORT,
    METHOD,
    VARIABLE,
    AttrRef,
    FileInfo,
    ImportBinding,
    NameRef,
    Symbol,
)
from .scanner import _SKIP_DIRS, _gitignored, _load_gitignore


def _require_tree_sitter():
    """Import tree-sitter lazily; raise a helpful error when the extra is missing."""
    try:
        from tree_sitter import Language, Parser  # noqa: F401

        import tree_sitter_typescript  # noqa: F401
        import tree_sitter_javascript  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "TypeScript scanning requires the 'typescript' extra: "
            "pip install deadcode-hunter[typescript]"
        ) from exc
    from tree_sitter import Language, Parser

    import tree_sitter_javascript
    import tree_sitter_typescript

    return Language, Parser, tree_sitter_typescript, tree_sitter_javascript


_PARSERS: dict[str, object] = {}


def _parser_for(ext: str):
    """Return a cached tree-sitter Parser for the given file extension."""
    global _PARSERS
    if ext not in _PARSERS:
        Language, Parser, ts, js = _require_tree_sitter()
        if ext == ".tsx":
            _PARSERS[ext] = Parser(Language(ts.language_tsx()))
        elif ext in (".js", ".jsx"):
            _PARSERS[ext] = Parser(Language(js.language()))
        else:  # .ts, .mts, .cts
            _PARSERS[ext] = Parser(Language(ts.language_typescript()))
    return _PARSERS[ext]


# ---------------------------------------------------------------------------
# tree-sitter node helpers
# ---------------------------------------------------------------------------


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _named(node) -> list:
    return [c for c in node.children if c.is_named]


def _child(node, *types):
    for c in node.children:
        if c.is_named and c.type in types:
            return c
    return None


def _children(node, *types) -> list:
    return [c for c in node.children if c.is_named and c.type in types]


_TS_EXTS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"})

# Member-expression roots that are platform globals, never references to
# something we could flag as dead.
_GLOBALS = frozenset(
    {"console", "process", "window", "globalThis", "document", "navigator"}
)

# Type nodes that name a referenceable type.
_TYPE_NODES = frozenset({"type_identifier"})
_CONTAINER_TYPE_NODES = frozenset(
    {
        "generic_type",
        "array_type",
        "tuple_type",
        "union_type",
        "intersection_type",
        "optional_type",
        "rest_type",
        "annotated_type",
        "type_arguments",
    }
)
# Definition node types, used by the prove-deletion overlap check.
_DEF_TYPES = frozenset(
    {
        "function_declaration",
        "class_declaration",
        "enum_declaration",
        "method_definition",
        "lexical_declaration",
        "variable_declarator",
        "public_field_definition",
        "import_statement",
        "export_statement",
    }
)

# Symbol kind -> tree-sitter definition node types, used by prove deletion
# to tell the target's own node apart from other definitions.
_KIND_TO_DEFS = {
    "function": {"function_declaration", "variable_declarator"},
    "method": {"method_definition"},
    "class": {"class_declaration", "enum_declaration"},
    "variable": {
        "lexical_declaration",
        "variable_declarator",
        "public_field_definition",
    },
    "import": {"import_statement"},
}


def _is_test_file(rel: Path) -> bool:
    """Heuristic: is this a TS/JS test file?"""
    name = rel.name
    if ".test." in name or ".spec." in name:
        return True
    return "__tests__" in rel.parts


def iter_typescript_files(root: Path) -> list[Path]:
    """Yield .ts/.tsx/.js/.jsx files under root, honoring skips and .gitignore."""
    gitignore = _load_gitignore(root)
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for filename in filenames:
            if Path(filename).suffix not in _TS_EXTS:
                continue
            full = Path(dirpath) / filename
            rel = full.relative_to(root)
            if _gitignored(rel.as_posix(), gitignore):
                continue
            out.append(full)
    return sorted(out)


class _TsFileScanner:
    """Walk one TypeScript/JavaScript file, filling a FileInfo."""

    def __init__(self, file_info: FileInfo, src: bytes, sid_counter: list[int]):
        self.fi = file_info
        self.src = src
        self.sid_counter = sid_counter
        self.file_dir = Path(file_info.path).parent
        # Local names exported via `export { a, b }` (resolved post-walk).
        self._exported_names: set[str] = set()
        # (scope, var) -> type name, for `x: Foo` params/locals and
        # `const x = new Foo()` initializers. Written straight into the
        # shared FileInfo so the resolver can use it.
        # Stack of enclosing class qualnames (for `this.` and super refs).
        self._class_stack: list[str] = []

    # -- symbol helpers ---------------------------------------------------

    def _symbol(
        self,
        name: str,
        kind: str,
        qualname: str,
        scope: str,
        node,
        **kwargs,
    ) -> Symbol:
        self.sid_counter[0] += 1
        sym = Symbol(
            sid=self.sid_counter[0],
            name=name,
            kind=kind,
            qualname=qualname,
            scope=scope,
            file=self.fi.path,
            lineno=node.start_point[0] + 1,
            end_lineno=node.end_point[0] + 1,
            **kwargs,
        )
        self.fi.symbols.append(sym)
        return sym

    def _name_ref(self, name: str, scope: str, node) -> None:
        self.fi.name_refs.append(
            NameRef(
                name=name,
                scope=scope,
                lineno=node.start_point[0] + 1,
            )
        )

    def _var_type(self, scope: str, name: str) -> str | None:
        return self.fi.var_types.get((scope, name))

    def _decorators(self, node) -> list[str]:
        out = []
        for c in _named(node):
            if c.type == "decorator":
                ident = _child(c, "identifier")
                out.append(_text(ident, self.src) if ident is not None else "decorator")
        return out

    # -- module / import resolution -----------------------------------------

    def _resolve_import(self, import_path: str) -> str | None:
        """Resolve a relative import to a dotted module; None for externals."""
        if not import_path.startswith("."):
            return None
        base = self.file_dir / import_path
        candidates = []
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            candidates.append(base.with_suffix(ext))
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            candidates.append(base / f"index{ext}")
        # base may already include an extension
        candidates.insert(0, base)
        for cand in candidates:
            cand_str = cand.as_posix()
            # strip leading ./ if present
            if cand_str.startswith("./"):
                cand_str = cand_str[2:]
            p = Path(cand_str)
            if p.suffix not in _TS_EXTS:
                continue
            # We cannot check the real filesystem relative to the scan
            # root here (we only have the relative dir); instead record
            # the module name — the resolver matches imports to modules by
            # this dotted name.
            no_suffix = p.with_suffix("")
            parts = list(no_suffix.parts)
            if parts and parts[-1] == "index":
                parts = parts[:-1]
            return ".".join(parts) if parts else None
        return None

    def _import(self, node, module: str) -> None:
        """Handle an import_statement node."""
        kids = _named(node)
        clause = _child(node, "import_clause")
        path_node = next(
            (c for c in kids if c.type == "string"), None
        )
        import_path = (
            _text(path_node, self.src).strip("\"'") if path_node is not None else ""
        )
        if clause is None:
            # `import "./side-effect";` — nothing is bound; never report it.
            return
        target_module = self._resolve_import(import_path)
        # default import: `import defExp from "./z"`
        default_ident = next(
            (c for c in _named(clause) if c.type == "identifier"), None
        )
        bindings: list[tuple[str, str | None]] = []  # (local_name, imported_symbol)
        if default_ident is not None:
            bindings.append((_text(default_ident, self.src), "default"))
        for c in _named(clause):
            if c.type == "namespace_import":
                ident = _child(c, "identifier")
                if ident is not None:
                    # `import * as ns` — the namespace object itself.
                    bindings.append((_text(ident, self.src), None))
            elif c.type == "named_imports":
                for spec in _children(c, "import_specifier"):
                    idents = _children(spec, "identifier")
                    if not idents:
                        continue
                    imported = _text(idents[0], self.src)
                    local = _text(idents[-1], self.src)
                    bindings.append((local, imported))
        for local_name, imported_symbol in bindings:
            self.sid_counter[0] += 1
            self.fi.symbols.append(
                Symbol(
                    sid=self.sid_counter[0],
                    name=local_name,
                    kind=IMPORT,
                    qualname=f"{module}.{local_name}",
                    scope=module,
                    file=self.fi.path,
                    lineno=node.start_point[0] + 1,
                    end_lineno=node.end_point[0] + 1,
                    binding=ImportBinding(
                        module=target_module or import_path,
                        symbol=imported_symbol,
                        lineno=node.start_point[0] + 1,
                    ),
                )
            )
            self.fi.module_imports[local_name] = ImportBinding(
                module=target_module or import_path,
                symbol=imported_symbol,
                lineno=node.start_point[0] + 1,
            )

    # -- declarations -------------------------------------------------------

    def _function(self, node, scope: str, exported: bool = False) -> None:
        name_node = _child(node, "identifier")
        if name_node is None:
            # Anonymous `export default function() {}` — walk the body only.
            body = _child(node, "statement_block")
            if body is not None:
                self._walk(body, scope)
            return
        name = _text(name_node, self.src)
        qualname = f"{scope}.{name}"
        sym = self._symbol(
            name,
            FUNCTION,
            qualname,
            scope,
            node,
            decorators=self._decorators(node),
            exported=exported,
        )
        self._params(_child(node, "formal_parameters"), qualname)
        self.fi.scope_kinds[qualname] = "function"
        body = _child(node, "statement_block")
        if body is not None:
            self._walk(body, qualname)

    def _class(self, node, scope: str, exported: bool = False) -> None:
        name_node = _child(node, "type_identifier")
        if name_node is None:
            return
        name = _text(name_node, self.src)
        qualname = f"{scope}.{name}"
        decorators = self._decorators(node)
        self._symbol(
            name,
            CLASS,
            qualname,
            scope,
            node,
            decorators=decorators,
            exported=exported,
        )
        self.fi.scope_kinds[qualname] = "class"
        self._class_stack.append(qualname)
        # extends clause -> superclass reference
        heritage = _child(node, "class_heritage")
        if heritage is not None:
            for c in _named(heritage):
                if c.type in ("identifier", "type_identifier"):
                    self._name_ref(_text(c, self.src), qualname, c)
                elif c.type == "member_expression":
                    self._member(c, qualname)
        body = _child(node, "class_body")
        if body is not None:
            for member in _named(body):
                if member.type == "method_definition":
                    self._method(member, qualname, exported=exported, class_decorators=decorators)
                elif member.type == "public_field_definition":
                    self._field(member, qualname, exported=exported, class_decorators=decorators)
                else:
                    self._walk(member, qualname)
        self._class_stack.pop()

    def _method(
        self,
        node,
        class_qualname: str,
        exported: bool = False,
        class_decorators: list | None = None,
    ) -> None:
        name_node = _child(node, "property_identifier", "identifier")
        if name_node is None:
            self._walk(node, class_qualname)
            return
        name = _text(name_node, self.src)
        qualname = f"{class_qualname}.{name}"
        self._symbol(
            name,
            METHOD,
            qualname,
            class_qualname,
            node,
            decorators=self._decorators(node) + (class_decorators or []),
            exported=exported,
        )
        self.fi.scope_kinds[qualname] = "function"
        self._params(_child(node, "formal_parameters"), qualname)
        body = _child(node, "statement_block")
        if body is not None:
            self._walk(body, qualname)

    def _field(
        self,
        node,
        class_qualname: str,
        exported: bool = False,
        class_decorators: list | None = None,
    ) -> None:
        name_node = _child(node, "property_identifier", "identifier")
        if name_node is None:
            self._walk(node, class_qualname)
            return
        name = _text(name_node, self.src)
        self._symbol(
            name,
            VARIABLE,
            f"{class_qualname}.{name}",
            class_qualname,
            node,
            decorators=class_decorators or [],
            exported=exported,
        )
        for c in _named(node):
            if c.type not in ("property_identifier", "identifier"):
                self._walk(c, class_qualname)

    def _enum(self, node, scope: str, exported: bool = False) -> None:
        name_node = next(
            (c for c in _named(node) if c.type in ("identifier", "type_identifier")),
            None,
        )
        if name_node is None:
            return
        name = _text(name_node, self.src)
        self._symbol(name, CLASS, f"{scope}.{name}", scope, node, exported=exported)

    def _params(self, params_node, scope: str) -> None:
        """Record parameter names and their annotated types."""
        if params_node is None:
            return
        for param in _named(params_node):
            ident = None
            if param.type in ("required_parameter", "optional_parameter"):
                ident = _child(param, "identifier")
                type_node = _child(param, "type_annotation")
            elif param.type == "identifier":
                ident = param
                type_node = None
            elif param.type == "assignment_pattern":
                ident = _child(param, "identifier")
                type_node = None
            else:
                # rest_pattern / object_pattern / array_pattern: walk for refs
                self._walk(param, scope)
                continue
            if ident is not None:
                name = _text(ident, self.src)
                if type_node is not None:
                    tname = self._simple_type_name(type_node)
                    if tname:
                        self.fi.var_types[(scope, name)] = tname
            # walk default values / annotations for nested references
            for c in _named(param):
                if c is not ident:
                    self._walk(c, scope)

    def _simple_type_name(self, type_annotation) -> str | None:
        """Extract `Foo` from a `: Foo` type_annotation; None for primitives."""
        for c in _named(type_annotation):
            if c.type == "type_identifier":
                return _text(c, self.src)
        return None

    def _type_ref(self, node, scope: str) -> None:
        """Emit NameRefs for type identifiers (keeps referenced types alive)."""
        for c in _named(node):
            if c.type == "type_identifier":
                self._name_ref(_text(c, self.src), scope, c)
            elif c.type in _CONTAINER_TYPE_NODES:
                self._type_ref(c, scope)
            elif c.type == "predefined_type":
                continue
            else:
                self._walk(c, scope)

    def _lexical(self, node, scope: str, exported: bool = False) -> None:
        """Handle const/let/var declarations."""
        for decl in _children(node, "variable_declarator"):
            name_node = _child(decl, "identifier")
            if name_node is None:
                # Destructuring: walk for nested references.
                self._walk(decl, scope)
                continue
            name = _text(name_node, self.src)
            qualname = f"{scope}.{name}"
            value = next(
                (
                    c
                    for c in _named(decl)
                    if c.type not in ("identifier", "type_annotation")
                ),
                None,
            )
            type_node = _child(decl, "type_annotation")
            if type_node is not None:
                tname = self._simple_type_name(type_node)
                if tname:
                    self.fi.var_types[(scope, name)] = tname
            if value is not None and value.type == "new_expression":
                ctor = _child(value, "identifier")
                if ctor is not None:
                    self.fi.var_types[(scope, name)] = _text(ctor, self.src)
            if value is not None and value.type in (
                "arrow_function",
                "function_expression",
            ):
                self._symbol(
                    name,
                    FUNCTION,
                    qualname,
                    scope,
                    decl,
                    decorators=self._decorators(node),
                    exported=exported,
                )
                self._params(_child(value, "formal_parameters"), qualname)
                body = _child(value, "statement_block")
                if body is not None:
                    self._walk(body, qualname)
                else:
                    # Concise body: `const f = () => expr`
                    for c in _named(value):
                        if c.type != "formal_parameters":
                            self._walk(c, qualname)
            else:
                self._symbol(
                    name,
                    VARIABLE,
                    qualname,
                    scope,
                    decl,
                    decorators=self._decorators(node),
                    exported=exported,
                )
                if value is not None:
                    self._walk(value, scope)

    # -- references -----------------------------------------------------------

    def _call(self, node, scope: str) -> None:
        """Handle call_expression: `f()`, `obj.m()`, `C.sm()`."""
        func = _child(
            node,
            "identifier",
            "member_expression",
            "call_expression",
            "parenthesized_expression",
        )
        args = _child(node, "arguments")
        if func is None:
            for c in _named(node):
                self._walk(c, scope)
            return
        if func.type == "identifier":
            name = _text(func, self.src)
            if name not in _GLOBALS:
                self._name_ref(name, scope, func)
        elif func.type == "member_expression":
            self._member(func, scope, is_call=True)
        else:
            self._walk(func, scope)
        if args is not None:
            for c in _named(args):
                self._walk(c, scope)

    def _member(self, node, scope: str, is_call: bool = False) -> None:
        """Handle member_expression: `obj.prop`, `this.x`, `C.CONST`."""
        kids = _named(node)
        if len(kids) < 2:
            for c in kids:
                self._walk(c, scope)
            return
        obj_node, prop_node = kids[0], kids[-1]
        prop = _text(prop_node, self.src)
        if obj_node.type == "this":
            base, call_base = "self", None
        elif obj_node.type == "identifier":
            oname = _text(obj_node, self.src)
            if oname in _GLOBALS:
                # `console.log(...)` etc: platform global, not a reference
                # to anything we could flag.
                for c in kids[2:]:
                    self._walk(c, scope)
                return
            # The base itself is read, e.g. `s` in `s.serve()`, `path` in
            # `path.join()` — this keeps variables and imports alive.
            self._name_ref(oname, scope, obj_node)
            if self._var_type(scope, oname) is not None:
                base, call_base = oname, None
            else:
                # Possibly a class name: `C.staticMethod()`.
                base, call_base = None, oname
        else:
            # Complex receiver: walk it, treat the property as unresolved.
            self._walk(obj_node, scope)
            base, call_base = None, None
        self.fi.attr_refs.append(
            AttrRef(
                attr=prop,
                base=base,
                dotted=None,
                call_base=call_base,
                scope=scope,
                lineno=node.start_point[0] + 1,
            )
        )
        for c in kids[2:]:
            self._walk(c, scope)

    def _new(self, node, scope: str) -> None:
        """Handle new_expression: `new Foo()` keeps Foo alive."""
        ctor = _child(node, "identifier", "member_expression")
        args = _child(node, "arguments")
        if ctor is not None:
            if ctor.type == "identifier":
                self._name_ref(_text(ctor, self.src), scope, ctor)
            else:
                self._member(ctor, scope)
        if args is not None:
            for c in _named(args):
                self._walk(c, scope)

    def _jsx(self, node, scope: str) -> None:
        """JSX elements: `<Foo />` / `<ns.Bar />` reference components."""
        opening = _child(node, "jsx_opening_element", "jsx_self_closing_element")
        if opening is None:
            for c in _named(node):
                self._walk(c, scope)
            return
        tag = next(iter(_named(opening)), None)
        if tag is not None:
            if tag.type == "identifier":
                name = _text(tag, self.src)
                # Lowercase tags are DOM intrinsics (<div>), not components.
                if name[:1].isupper():
                    self._name_ref(name, scope, tag)
            elif tag.type == "member_expression":
                self._member(tag, scope)
        for c in _named(opening):
            if c is not tag:
                self._walk(c, scope)
        # Walk children (nested elements, expressions).
        for c in _named(node):
            if c is not opening and c.type != "jsx_closing_element":
                self._walk(c, scope)

    # -- export handling ------------------------------------------------------

    def _export_statement(self, node, module: str, scope: str) -> None:
        """Handle export_statement: mark exported symbols, record specifiers."""
        kids = _named(node)
        if not kids:
            return
        first = kids[0]
        if first.type == "export_clause":
            for spec in _children(first, "export_specifier"):
                idents = _children(spec, "identifier")
                if idents:
                    # `export { a, b as c }` — the first identifier is the
                    # local name.
                    self._exported_names.add(_text(idents[0], self.src))
            # `export * from "./x"` has no export_clause idents; the star
            # re-export is recorded but its names are unknowable.
            return
        if first.type == "string":
            # `export * from "./x"` — nothing local to mark.
            return
        # `export <declaration>` or `export default <declaration|expr>`.
        decl = first
        if decl.type == "function_declaration":
            self._function(decl, scope, exported=True)
        elif decl.type == "class_declaration":
            self._class(decl, scope, exported=True)
        elif decl.type == "enum_declaration":
            self._enum(decl, scope, exported=True)
        elif decl.type == "lexical_declaration":
            self._lexical(decl, scope, exported=True)
        elif decl.type == "interface_declaration":
            pass  # contract, never reported
        elif decl.type == "type_alias_declaration":
            pass  # contract, never reported
        else:
            # `export default <expression>` — walk it for references.
            self._walk(decl, scope)

    def _apply_export_marks(self) -> None:
        """Mark symbols named in `export { a, b }` as exported."""
        if not self._exported_names:
            return
        for sym in self.fi.symbols:
            # Only module-scope symbols (not class members).
            if sym.name in self._exported_names and sym.scope == self.fi.module:
                sym.exported = True

    # -- main walk ------------------------------------------------------------

    def _walk(self, node, scope: str) -> None:
        t = node.type
        if t == "function_declaration":
            self._function(node, scope)
        elif t == "class_declaration":
            self._class(node, scope)
        elif t == "enum_declaration":
            self._enum(node, scope)
        elif t in ("interface_declaration", "type_alias_declaration"):
            # Contracts: walk for type references only, never report.
            for c in _named(node):
                if c.type in _TYPE_NODES or c.type in _CONTAINER_TYPE_NODES:
                    self._type_ref(c, scope)
                elif c.type not in ("type_identifier",):
                    self._walk(c, scope)
        elif t == "lexical_declaration":
            self._lexical(node, scope)
        elif t == "import_statement":
            self._import(node, scope)
        elif t == "export_statement":
            self._export_statement(node, scope, scope)
        elif t == "call_expression":
            self._call(node, scope)
        elif t == "new_expression":
            self._new(node, scope)
        elif t == "member_expression":
            self._member(node, scope)
        elif t in ("jsx_element", "jsx_self_closing_element"):
            self._jsx(node, scope)
        elif t == "type_annotation":
            tname = self._simple_type_name(node)
            if tname:
                self._name_ref(tname, scope, node)
        elif t == "identifier":
            # Bare identifier read (variable use, shorthand, etc.).
            self._name_ref(_text(node, self.src), scope, node)
        elif t == "shorthand_property_identifier":
            # `{ foo }` reads foo.
            self._name_ref(_text(node, self.src), scope, node)
        else:
            for c in _named(node):
                self._walk(c, scope)

    def run(self, tree, module: str) -> None:
        self.fi.scope_kinds[module] = "module"
        self._walk(tree.root_node, module)
        self._apply_export_marks()


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------


def _module_name(rel: Path) -> str:
    """Dotted module name for a file relative to the scan root."""
    no_suffix = rel.with_suffix("")
    parts = list(no_suffix.parts)
    if parts and parts[-1] == "index":
        parts = parts[:-1]
    return ".".join(parts)


def scan_typescript_file(
    root: Path, path: Path, sid_counter: list[int]
) -> FileInfo | None:
    """Parse one .ts/.tsx/.js/.jsx file and extract its symbols and references."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    src = raw.decode("utf-8", errors="replace").encode("utf-8")
    rel = path.relative_to(root)
    module = _module_name(rel)
    tree = _parser_for(path.suffix).parse(src)
    file_info = FileInfo(
        path=rel.as_posix(),
        module=module,
        is_init=False,
        is_test=_is_test_file(rel),
    )
    _TsFileScanner(file_info, src, sid_counter).run(tree, module)
    return file_info


def scan_typescript_directory(
    root: Path | str, sid_counter: list[int] | None = None
) -> dict[str, FileInfo]:
    """Scan a directory (or a single file) for TypeScript/JavaScript sources."""
    _require_tree_sitter()  # fail fast with the pip-install hint
    root_path = Path(root)
    if sid_counter is None:
        sid_counter = [0]
    if root_path.is_file():
        paths = [root_path]
        root_path = root_path.parent
    else:
        paths = iter_typescript_files(root_path)
    files: dict[str, FileInfo] = {}
    for path in paths:
        info = scan_typescript_file(root_path, path, sid_counter)
        if info is not None:
            files[info.path] = info
    return files


# ---------------------------------------------------------------------------
# prove support: delete a TypeScript symbol by line span
# ---------------------------------------------------------------------------


def _tree_has_error(node) -> bool:
    if node.type == "ERROR" or node.is_missing:
        return True
    return any(_tree_has_error(c) for c in node.children if c.is_named)


def _definition_spans(node, spans: list) -> None:
    if node.type in _DEF_TYPES and node.is_named:
        spans.append((node.type, node.start_point[0] + 1, node.end_point[0] + 1))
    for c in node.children:
        if c.is_named:
            _definition_spans(c, spans)


def _import_specifier_count(root, lineno: int) -> int:
    """Number of import_specifiers in the import_statement at `lineno`."""
    found = []

    def visit(node):
        if node.type == "import_statement" and node.start_point[0] + 1 == lineno:
            found.append(node)
        for c in node.children:
            if c.is_named:
                visit(c)

    visit(root)
    count = 0
    for node in found:
        for c in _named(node):
            if c.type == "import_clause":
                for sub in _named(c):
                    if sub.type == "named_imports":
                        count += len(_children(sub, "import_specifier"))
    return count


def delete_typescript_symbol(path: Path, symbol: Symbol) -> tuple[bool, str]:
    """Delete a TypeScript/JavaScript symbol's definition from `path`.

    Returns (True, "") on success, or (False, reason) when the deletion
    cannot be done cleanly -- the caller should skip the candidate.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read file: {exc}"
    parser = _parser_for(path.suffix if path.suffix in _TS_EXTS else ".ts")
    tree = parser.parse(source.encode("utf-8"))
    if _tree_has_error(tree.root_node):
        return False, "cannot parse file"
    lines = source.splitlines(keepends=True)
    start, end = symbol.lineno, symbol.end_lineno
    if not 1 <= start <= end <= len(lines):
        return False, "symbol location out of range"

    if symbol.kind == "import":
        idx = start - 1
        span_text = "".join(lines[idx:end])
        if "import" not in span_text or symbol.name not in span_text:
            return False, f"could not locate import '{symbol.name}'"
        if _import_specifier_count(tree.root_node, start) > 1:
            # `import { a, b }` with only one dead: refuse (rewriting the
            # specifier list is left for a future version).
            return False, "import shares its statement with other imports"
        new_lines = lines[:idx] + lines[end:]
    else:
        spans: list[tuple[str, int, int]] = []
        _definition_spans(tree.root_node, spans)
        target_types = _KIND_TO_DEFS.get(symbol.kind, set())
        for dtype, os_, oe in spans:
            if (os_, oe) == (start, end) and dtype in target_types:
                continue  # the target's own definition node
            intersects = not (oe < start or os_ > end)
            strictly_inside = os_ >= start and oe <= end and (os_, oe) != (start, end)
            strictly_contains = os_ <= start and oe >= end and (os_, oe) != (start, end)
            if intersects and not strictly_inside and not strictly_contains:
                # Another definition shares these lines: refuse rather than
                # over-delete.
                return False, "definition shares lines with another definition"
        if symbol.kind == "variable":
            # `const a = 1, b = 2;` — refuse multi-declarator lines.
            for dtype, os_, oe in spans:
                if (
                    dtype == "lexical_declaration"
                    and os_ == start
                    and (os_, oe) != (start, end)
                ):
                    return False, "declaration also defines other variables"
        new_lines = lines[: start - 1] + lines[end:]

    new_source = "".join(new_lines)
    tree2 = parser.parse(new_source.encode("utf-8"))
    if _tree_has_error(tree2.root_node):
        return False, "deletion breaks syntax"
    try:
        path.write_text(new_source, encoding="utf-8")
    except OSError as exc:
        return False, f"cannot write file: {exc}"
    return True, ""
