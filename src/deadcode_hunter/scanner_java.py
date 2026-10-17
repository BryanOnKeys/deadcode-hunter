"""Java dead-code scanner built on tree-sitter.

Produces the same :class:`FileInfo` structures as the Python scanner, so the
shared resolver/investigator pipeline works unchanged across languages.

tree-sitter is an optional dependency::

    pip install deadcode-hunter[java]

Design notes (Java -> shared-model mapping):

- ``package com.example;`` becomes the FileInfo ``module`` (``"com.example"``),
  so cross-file import resolution works exactly like Python's.
- ``import com.foo.Bar;`` becomes an import symbol with an
  ``ImportBinding(module="com.foo", symbol="Bar")`` binding, like a Python
  ``from com.foo import Bar``. ``import pkg.*;`` is recorded as a star import
  (no import symbol: we cannot know which names it provides, so flagging it
  would be a false positive).
- Constructors are symbols of kind ``method`` named after their class, stored
  at the *same scope* as the class itself. A ``new Foo()`` reference therefore
  marks both the class and its constructor alive.
- ``this.x`` is mapped to the resolver's ``self`` convention, so
  ``this.helper()`` resolves to the enclosing class's method.
- ``main`` methods (and the class containing one) are entry points and are
  never reported. ``@Override`` methods are treated as alive by construction
  (they are wired to their supertype); other annotations only downgrade to
  medium, like Python decorators.
- Reflection string literals (``Class.forName("com.foo.Bar")``,
  ``getMethod("name")``, ...) are recorded as dynamic names, downgrading the
  targets to medium instead of high.
"""

from __future__ import annotations

import os
from pathlib import Path

from .models import (
    CLASS,
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

        import tree_sitter_java  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Java scanning requires the 'java' extra: pip install deadcode-hunter[java]"
        ) from exc
    from tree_sitter import Language, Parser

    import tree_sitter_java

    return Language, Parser, tree_sitter_java


_PARSER = None


def _parser():
    global _PARSER
    if _PARSER is None:
        Language, Parser, tree_sitter_java = _require_tree_sitter()
        _PARSER = Parser(Language(tree_sitter_java.language()))
    return _PARSER


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


_TYPE_DECLS = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)
_BODY_NODES = frozenset(
    {"class_body", "interface_body", "enum_body", "annotation_type_body"}
)
# Type nodes that can name a referenceable type.
_REF_TYPES = frozenset({"type_identifier", "scoped_type_identifier"})
_CONTAINER_TYPES = frozenset(
    {
        "generic_type",
        "array_type",
        "wildcard",
        "type_arguments",
        "upper_bound",
        "lower_bound",
        "annotated_type",
    }
)

# Definition node types, used by the prove-deletion overlap check.
_DEF_TYPES = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
        "field_declaration",
        "import_declaration",
    }
)

# Methods whose string-literal argument names a reflectively accessed member.
_REFLECTIVE_METHODS = frozenset(
    {
        "forName",
        "getMethod",
        "getDeclaredMethod",
        "getField",
        "getDeclaredField",
        "loadClass",
    }
)

_JAVA_SKIP_DIRS = _SKIP_DIRS | frozenset({"target", "out", ".gradle"})


def _is_test_file(rel: Path) -> bool:
    name = rel.name
    if name.startswith("Test") or name.endswith(("Test.java", "Tests.java")):
        return True
    return any(part.lower() in ("test", "tests") for part in rel.parts[:-1])


def iter_java_files(root: Path) -> list[Path]:
    """Recursively find .java files, skipping build dirs and gitignored paths."""
    patterns = _load_gitignore(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _JAVA_SKIP_DIRS
            and not _gitignored(
                (rel_dir + "/" + d).lstrip("./") if rel_dir != "." else d, patterns
            )
        ]
        for filename in filenames:
            if not filename.endswith(".java"):
                continue
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            if _gitignored(rel, patterns):
                continue
            found.append(full)
    return sorted(found)


class _JavaFileScanner:
    """Extract symbols and references from one parsed Java file."""

    def __init__(self, file_info: FileInfo, src: bytes, sid_counter: list[int]):
        self.fi = file_info
        self.src = src
        self.sid_counter = sid_counter
        # Stack of (class qualname, superclass simple name) for super(...) calls.
        self._class_stack: list[tuple[str, str | None]] = []

    # -- small builders ----------------------------------------------------

    def _symbol(self, name, kind, qualname, scope, node, **kwargs) -> Symbol:
        self.sid_counter[0] += 1
        sp, ep = node.start_point, node.end_point
        return Symbol(
            sid=self.sid_counter[0],
            name=name,
            kind=kind,
            qualname=qualname,
            scope=scope,
            file=self.fi.path,
            lineno=sp[0] + 1,
            end_lineno=ep[0] + 1,
            **kwargs,
        )

    def _name_ref(self, name: str, scope: str, node, is_store: bool = False) -> None:
        if not name:
            return
        self.fi.name_refs.append(
            NameRef(
                name=name,
                scope=scope,
                lineno=node.start_point[0] + 1,
                is_store=is_store,
            )
        )

    def _simple_name(self, type_node) -> str:
        """Best-effort simple name of a type node ('' when unknown)."""
        t = type_node.type
        if t == "type_identifier":
            return _text(type_node, self.src).strip()
        if t == "scoped_type_identifier":
            return _text(type_node, self.src).replace(" ", "").split(".")[-1].strip()
        if t in _CONTAINER_TYPES:
            for c in _named(type_node):
                name = self._simple_name(c)
                if name:
                    return name
        return ""

    def _type_ref(self, type_node, scope: str) -> None:
        """Emit NameRefs for a type usage, descending into generics/arrays."""
        t = type_node.type
        if t in _REF_TYPES:
            self._name_ref(self._simple_name(type_node), scope, type_node)
        elif t in _CONTAINER_TYPES:
            for c in _named(type_node):
                self._type_ref(c, scope)
        # primitives / void: nothing to reference

    def _var_type(self, scope: str, name: str) -> str | None:
        parts = scope.split(".")
        for i in range(len(parts), 0, -1):
            key = (".".join(parts[:i]), name)
            if key in self.fi.var_types:
                return self.fi.var_types[key]
        return None

    def _modifier_set(self, node) -> set[str]:
        mods = _child(node, "modifiers")
        if mods is None:
            return set()
        # Keyword modifiers are anonymous children; annotations are named.
        return {_text(c, self.src) for c in mods.children if not c.is_named}

    def _annotations(self, node) -> list[str]:
        mods = _child(node, "modifiers")
        if mods is None:
            return []
        out = []
        for c in _named(mods):
            if c.type in ("marker_annotation", "annotation"):
                text = _text(c, self.src).lstrip()
                if text.startswith("@"):
                    out.append(text[1:].split("(")[0].strip())
        return out

    # -- top level ----------------------------------------------------------

    def run(self, tree) -> None:
        root = tree.root_node
        package = ""
        for child in _named(root):
            if child.type == "package_declaration":
                path_node = _child(child, "scoped_identifier", "identifier")
                if path_node is not None:
                    package = (
                        _text(path_node, self.src).replace(" ", "").strip()
                    )
        self.fi.module = package
        self.fi.scope_kinds[package] = "module"
        for child in _named(root):
            if child.type == "import_declaration":
                self._import(child, package)
            elif child.type in _TYPE_DECLS:
                self._type_decl(child, package)

    def _import(self, node, package: str) -> None:
        lineno = node.start_point[0] + 1
        children = node.children
        has_asterisk = any(c.type == "asterisk" for c in children)
        path_node = _child(node, "scoped_identifier", "identifier")
        if path_node is None:
            return
        dotted = _text(path_node, self.src).replace(" ", "").strip()
        if has_asterisk:
            # `import pkg.*;` — we cannot know which names it provides, so no
            # import symbol (flagging it would be a false positive). The star
            # import still feeds the resolver's weak-reference machinery.
            self.fi.star_imports.append(dotted)
            return
        parts = dotted.split(".")
        local, mod = parts[-1], ".".join(parts[:-1])
        # `import com.foo.Bar;` and `import static com.foo.Bar.BAZ;` share the
        # same shape: local name bound from module `mod`.
        binding = ImportBinding(module=mod, symbol=local, lineno=lineno)
        self.fi.module_imports[local] = binding
        qualname = f"{package}.{local}" if package else local
        self.fi.symbols.append(
            self._symbol(local, IMPORT, qualname, package, node, binding=binding)
        )

    # -- type declarations ---------------------------------------------------

    def _type_decl(self, node, scope: str) -> None:
        name_node = _child(node, "identifier")
        if name_node is None:
            return
        name = _text(name_node, self.src)
        qualname = f"{scope}.{name}" if scope else name
        modifiers = self._modifier_set(node)
        annotations = self._annotations(node)
        sym = self._symbol(
            name,
            CLASS,
            qualname,
            scope,
            node,
            decorators=annotations,
            is_abstract=(
                "abstract" in modifiers
                or node.type in ("interface_declaration", "annotation_type_declaration")
            ),
            public_api=("public" in modifiers),
        )
        self.fi.symbols.append(sym)
        self.fi.scope_kinds[qualname] = "class"
        superclass: str | None = None
        for clause in _children(node, "superclass", "super_interfaces"):
            for tn in _named(clause):
                if tn.type in _REF_TYPES or tn.type in _CONTAINER_TYPES:
                    self._type_ref(tn, qualname)
            if clause.type == "superclass":
                for tn in _named(clause):
                    simple = self._simple_name(tn)
                    if simple:
                        superclass = simple
                        break
        self._class_stack.append((qualname, superclass))
        try:
            body = _child(node, *_BODY_NODES)
            if body is not None and self._class_body(
                body, qualname, node.type in ("interface_declaration", "annotation_type_declaration")
            ):
                # A class containing main() is itself a program entry point.
                sym.is_entry_point = True
        finally:
            self._class_stack.pop()

    def _class_body(self, body, class_qualname: str, in_interface: bool = False) -> bool:
        """Walk a type body. Returns True when it declares a main() method."""
        has_main = False
        for child in _named(body):
            t = child.type
            if t == "method_declaration":
                if self._method(child, class_qualname, in_interface):
                    has_main = True
            elif t == "constructor_declaration":
                self._constructor(child, class_qualname)
            elif t == "field_declaration":
                self._field(child, class_qualname)
            elif t in _TYPE_DECLS:
                self._type_decl(child, class_qualname)
            elif t == "annotation_type_element_declaration":
                self._annotation_element(child, class_qualname)
            elif t == "static_initializer":
                blk = _child(child, "block")
                self._walk(blk if blk is not None else child, class_qualname)
            elif t == "block":  # instance initializer
                self._walk(child, class_qualname)
            elif t == "enum_constant":
                for sub in _named(child):
                    if sub.type in _BODY_NODES:
                        self._class_body(sub, class_qualname)
            # enum_constant names: implicitly referenced, not tracked
        return has_main

    def _method(self, node, class_qualname: str, in_interface: bool = False) -> bool:
        """Record a method. Returns True when it is a main() entry point."""
        name_node = _child(node, "identifier")
        if name_node is None:
            return False
        name = _text(name_node, self.src)
        qualname = f"{class_qualname}.{name}"
        modifiers = self._modifier_set(node)
        annotations = self._annotations(node)
        for c in _named(node):
            if c.type == "throws":
                for tn in _named(c):
                    if tn.type in _REF_TYPES or tn.type in _CONTAINER_TYPES:
                        self._type_ref(tn, qualname)
        params = _child(node, "formal_parameters")
        if params is not None:
            self._params(params, qualname)
        is_main = name == "main" and "static" in modifiers
        block = _child(node, "block")
        sym = self._symbol(
            name,
            METHOD,
            qualname,
            class_qualname,
            node,
            decorators=annotations,
            # @Override methods are wired to their supertype: alive by
            # construction. Interface members are implicitly abstract.
            is_abstract=(
                "abstract" in modifiers
                or "Override" in annotations
                or (in_interface and block is None)
            ),
            is_entry_point=is_main,
            public_api=("public" in modifiers),
        )
        self.fi.symbols.append(sym)
        self.fi.scope_kinds[qualname] = "function"
        if block is not None:
            self._walk(block, qualname)
        return is_main

    def _constructor(self, node, class_qualname: str) -> None:
        # Constructors live at the same scope as their class, so `new Foo()`
        # marks both the class and the constructor alive.
        class_scope = class_qualname.rsplit(".", 1)[0]
        if not class_scope:
            class_scope = self.fi.module
        class_name = class_qualname.rsplit(".", 1)[-1]
        qualname = f"{class_qualname}.<init>"
        modifiers = self._modifier_set(node)
        annotations = self._annotations(node)
        sym = self._symbol(
            class_name,
            METHOD,
            qualname,
            class_scope,
            node,
            decorators=annotations,
            public_api=("public" in modifiers),
        )
        self.fi.symbols.append(sym)
        self.fi.scope_kinds[qualname] = "function"
        params = _child(node, "formal_parameters")
        if params is not None:
            self._params(params, qualname)
        body = _child(node, "constructor_body")
        if body is not None:
            self._walk(body, qualname)

    def _field(self, node, class_qualname: str) -> None:
        type_node = next(
            (c for c in _named(node) if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES),
            None,
        )
        if type_node is not None:
            self._type_ref(type_node, class_qualname)
        modifiers = self._modifier_set(node)
        for decl in _children(node, "variable_declarator"):
            name_node = _child(decl, "identifier")
            if name_node is None:
                continue
            name = _text(name_node, self.src)
            if type_node is not None:
                self.fi.var_types[(class_qualname, name)] = self._simple_name(type_node)
            # The symbol spans the whole declaration so prove can delete it.
            self.fi.symbols.append(
                self._symbol(
                    name,
                    VARIABLE,
                    f"{class_qualname}.{name}",
                    class_qualname,
                    node,
                    public_api=("public" in modifiers),
                )
            )
            for c in _named(decl):
                if c.type == "identifier":
                    continue
                self._walk(c, class_qualname)

    def _annotation_element(self, node, class_qualname: str) -> None:
        name_node = _child(node, "identifier")
        if name_node is None:
            return
        name = _text(name_node, self.src)
        # Annotation members are implicitly abstract: implemented by proxies.
        self.fi.symbols.append(
            self._symbol(
                name,
                METHOD,
                f"{class_qualname}.{name}",
                class_qualname,
                node,
                is_abstract=True,
            )
        )

    def _params(self, params_node, scope: str) -> None:
        for child in _named(params_node):
            name_node = _child(child, "identifier")
            if name_node is None:
                continue
            name = _text(name_node, self.src)
            type_node = next(
                (c for c in _named(child) if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES),
                None,
            )
            if type_node is not None:
                self._type_ref(type_node, scope)
                self.fi.var_types[(scope, name)] = self._simple_name(type_node)

    # -- statements and expressions -------------------------------------------

    def _declarator(self, decl, scope: str, type_node) -> None:
        name_node = _child(decl, "identifier")
        if name_node is None:
            return
        name = _text(name_node, self.src)
        if type_node is not None:
            self.fi.var_types[(scope, name)] = self._simple_name(type_node)
        for c in _named(decl):
            if c.type == "identifier":
                continue
            self._walk(c, scope)

    def _walk(self, node, scope: str) -> None:
        if node is None:
            return
        t = node.type
        if t == "local_variable_declaration":
            type_node = next(
                (c for c in _named(node) if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES),
                None,
            )
            if type_node is not None:
                self._type_ref(type_node, scope)
            for decl in _children(node, "variable_declarator"):
                self._declarator(decl, scope, type_node)
            return
        if t == "variable_declarator":
            # e.g. inside a resource_specification: the type is a sibling.
            parent = node.parent
            type_node = None
            if parent is not None:
                type_node = next(
                    (c for c in _named(parent) if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES),
                    None,
                )
            self._declarator(node, scope, type_node)
            return
        if t == "resource":
            # try-with-resources: (Type name = expr)
            type_node = next(
                (c for c in _named(node) if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES),
                None,
            )
            if type_node is not None:
                self._type_ref(type_node, scope)
                name_node = _child(node, "identifier")
                if name_node is not None:
                    self.fi.var_types[(scope, _text(name_node, self.src))] = self._simple_name(
                        type_node
                    )
            for c in _named(node):
                if c.type == "identifier" or c is type_node:
                    continue
                self._walk(c, scope)
            return
        if t == "enhanced_for_statement":
            kids = _named(node)
            type_node = next(
                (c for c in kids if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES),
                None,
            )
            name_node = _child(node, "identifier")
            if type_node is not None and name_node is not None:
                self._type_ref(type_node, scope)
                self.fi.var_types[(scope, _text(name_node, self.src))] = self._simple_name(
                    type_node
                )
            for c in kids:
                if c is type_node or c is name_node:
                    continue
                self._walk(c, scope)
            return
        if t == "method_invocation":
            self._invocation(node, scope)
            return
        if t == "object_creation_expression":
            self._creation(node, scope)
            return
        if t == "array_creation_expression":
            for c in _named(node):
                if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES:
                    self._type_ref(c, scope)
                else:
                    self._walk(c, scope)
            return
        if t == "field_access":
            self._field_access(node, scope)
            return
        if t == "explicit_constructor_invocation":
            self._ctor_invocation(node, scope)
            return
        if t == "assignment_expression":
            self._assignment(node, scope)
            return
        if t == "cast_expression":
            for c in _named(node):
                if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES:
                    self._type_ref(c, scope)
                else:
                    self._walk(c, scope)
            return
        if t == "instanceof_expression":
            for c in _named(node):
                if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES:
                    self._type_ref(c, scope)
                else:
                    self._walk(c, scope)
            return
        if t == "catch_clause":
            for c in _named(node):
                if c.type == "catch_formal_parameter":
                    self._catch_param(c, scope)
                else:
                    self._walk(c, scope)
            return
        if t in _TYPE_DECLS:
            # local class inside a method body
            self._type_decl(node, scope)
            return
        if t == "identifier":
            # bare read in expression position (e.g. `counter++`, `return x;`)
            self._name_ref(_text(node, self.src), scope, node)
            return
        for c in _named(node):
            self._walk(c, scope)

    def _catch_param(self, node, scope: str) -> None:
        name_node = _child(node, "identifier")
        type_node = next(
            (c for c in _named(node) if c.type in _REF_TYPES or c.type in _CONTAINER_TYPES),
            None,
        )
        if type_node is not None:
            self._type_ref(type_node, scope)
            if name_node is not None:
                self.fi.var_types[(scope, _text(name_node, self.src))] = self._simple_name(
                    type_node
                )

    def _assignment(self, node, scope: str) -> None:
        kids = _named(node)
        if not kids:
            return
        left, rest = kids[0], kids[1:]
        # A store is not proof of read (mirrors the Python scanner).
        if left.type == "identifier":
            self._name_ref(_text(left, self.src), scope, left, is_store=True)
        else:
            self._walk(left, scope)
        for c in rest:
            self._walk(c, scope)

    def _invocation(self, node, scope: str) -> None:
        kids = _named(node)
        arg_list = _child(node, "argument_list")
        name_node, obj_node = None, None
        if arg_list is not None and arg_list in kids:
            idx = kids.index(arg_list)
            for j in range(idx - 1, -1, -1):
                if kids[j].type == "identifier":
                    name_node = kids[j]
                    obj_node = kids[j - 1] if j > 0 else None
                    break
        if name_node is None:
            for c in kids:
                self._walk(c, scope)
            return
        name = _text(name_node, self.src)
        if name in _REFLECTIVE_METHODS and arg_list is not None:
            args = _named(arg_list)
            if args and args[0].type == "string_literal":
                lit = _text(args[0], self.src).strip()
                if len(lit) >= 2 and lit.startswith('"') and lit.endswith('"'):
                    lit = lit[1:-1]
                simple = lit.split(".")[-1].strip()
                if simple:
                    # e.g. Class.forName("com.foo.Bar") -> "Bar",
                    # getMethod("greet") -> "greet"
                    self.fi.dynamic_names.add(simple)
        if obj_node is None or obj_node.type == "this":
            base, call_base = "self", None
            if obj_node is None:
                # Bare `name(...)` may resolve through a static import
                # (`import static java.lang.Math.max; max(x, y)`), which only
                # a name reference can reach.
                self._name_ref(name, scope, name_node)
        elif obj_node.type == "identifier":
            oname = _text(obj_node, self.src)
            if self._var_type(scope, oname) is not None:
                base, call_base = oname, None
            else:
                # Possibly a class name: `Klass.staticMethod()`.
                base, call_base = None, oname
        else:
            # Complex receiver (field access, chained call, ...): walk it for
            # nested references and treat the method name as unresolved.
            self._walk(obj_node, scope)
            base, call_base = None, None
        self.fi.attr_refs.append(
            AttrRef(
                attr=name,
                base=base,
                dotted=None,
                call_base=call_base,
                scope=scope,
                lineno=node.start_point[0] + 1,
            )
        )
        if arg_list is not None:
            for c in _named(arg_list):
                self._walk(c, scope)

    def _creation(self, node, scope: str) -> None:
        for c in _named(node):
            if c.type == "argument_list":
                for a in _named(c):
                    self._walk(a, scope)
            elif c.type in _REF_TYPES or c.type in _CONTAINER_TYPES:
                self._type_ref(c, scope)
            elif c.type == "class_body":
                # Anonymous class: walk its members in a synthetic scope.
                self._class_body(c, f"{scope}.<anonymous>")
            # 'new' is anonymous

    def _field_access(self, node, scope: str) -> None:
        kids = _named(node)
        if not kids:
            return
        field_node = kids[-1]
        field = _text(field_node, self.src)
        obj_node = kids[-2] if len(kids) >= 2 else None
        if (
            field == "class"
            and obj_node is not None
            and obj_node.type in ("identifier", "type_identifier", "scoped_type_identifier")
        ):
            # `Foo.class` is a real reference to Foo.
            self._name_ref(_text(obj_node, self.src).split(".")[-1].strip(), scope, obj_node)
            return
        if obj_node is None or obj_node.type == "this":
            base, call_base = "self", None
        elif obj_node.type == "identifier":
            oname = _text(obj_node, self.src)
            if self._var_type(scope, oname) is not None:
                base, call_base = oname, None
            else:
                # Possibly a class name: `Klass.CONSTANT`.
                base, call_base = None, oname
        else:
            self._walk(obj_node, scope)
            base, call_base = None, None
        self.fi.attr_refs.append(
            AttrRef(
                attr=field,
                base=base,
                dotted=None,
                call_base=call_base,
                scope=scope,
                lineno=node.start_point[0] + 1,
            )
        )

    def _ctor_invocation(self, node, scope: str) -> None:
        # `this(...)` / `super(...)` inside a constructor body.
        kinds = {c.type for c in node.children}
        if "super" in kinds and self._class_stack:
            _, superclass = self._class_stack[-1]
            if superclass:
                self._name_ref(superclass, scope, node)
        arg_list = _child(node, "argument_list")
        if arg_list is not None:
            for c in _named(arg_list):
                self._walk(c, scope)


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------


def scan_java_file(root: Path, path: Path, sid_counter: list[int]) -> FileInfo | None:
    """Parse one .java file and extract its symbols and references."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    src = raw.decode("utf-8", errors="replace").encode("utf-8")
    rel = path.relative_to(root).as_posix()
    tree = _parser().parse(src)
    file_info = FileInfo(
        path=rel,
        module="",
        is_init=False,
        is_test=_is_test_file(path.relative_to(root)),
    )
    _JavaFileScanner(file_info, src, sid_counter).run(tree)
    return file_info


def scan_java_directory(
    root: Path | str, sid_counter: list[int] | None = None
) -> dict[str, FileInfo]:
    """Scan a directory (or a single file) for Java sources."""
    _require_tree_sitter()  # fail fast with the pip-install hint
    root_path = Path(root)
    if sid_counter is None:
        sid_counter = [0]
    if root_path.is_file():
        paths = [root_path]
        root_path = root_path.parent
    else:
        paths = iter_java_files(root_path)
    files: dict[str, FileInfo] = {}
    for path in paths:
        info = scan_java_file(root_path, path, sid_counter)
        if info is not None:
            files[info.path] = info
    return files


# ---------------------------------------------------------------------------
# prove support: delete a Java symbol by line span
# ---------------------------------------------------------------------------


# Symbol kind -> tree-sitter definition node types, used by prove deletion
# to tell the target's own node apart from other definitions.
_KIND_TO_DEFS = {
    "method": {"method_declaration", "constructor_declaration"},
    "class": {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    },
    "variable": {"field_declaration"},
    "import": {"import_declaration"},
}


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


def delete_java_symbol(path: Path, symbol: Symbol) -> tuple[bool, str]:
    """Delete a Java symbol's definition from `path` by line span.

    Returns (True, "") on success, or (False, reason) when the deletion
    cannot be done cleanly -- the caller should skip the candidate.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read file: {exc}"
    parser = _parser()
    tree = parser.parse(source.encode("utf-8"))
    if _tree_has_error(tree.root_node):
        return False, "cannot parse file"
    lines = source.splitlines(keepends=True)
    start, end = symbol.lineno, symbol.end_lineno
    if not 1 <= start <= end <= len(lines):
        return False, "symbol location out of range"

    if symbol.kind == "import":
        idx = start - 1
        line = lines[idx]
        if "import" not in line or symbol.name not in line:
            return False, f"could not locate import '{symbol.name}'"
        new_lines = lines[:idx] + lines[idx + 1 :]
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
                # Another definition shares these lines (e.g. a one-liner
                # `class A { void m() {} }`): refuse rather than over-delete.
                return False, "definition shares lines with another definition"
        if symbol.kind == "variable" and _field_has_siblings(
            tree.root_node, start, symbol.name
        ):
            return False, "declaration also defines other fields"
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


def _field_has_siblings(root, lineno: int, name: str) -> bool:
    """True when the field_declaration starting at `lineno` declares other fields."""
    found = []

    def visit(node):
        if node.type == "field_declaration" and node.start_point[0] + 1 == lineno:
            found.append(node)
        for c in node.children:
            if c.is_named:
                visit(c)

    visit(root)
    for node in found:
        declarators = _children(node, "variable_declarator")
        if len(declarators) > 1:
            return True
    return False
