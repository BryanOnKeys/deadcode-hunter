"""Cross-file reference resolution.

Turns raw name/attribute references into strong references (proven use)
or weak references (a name appears in an attribute access we could not
resolve — it *might* be a use, so the target can no longer be high
confidence dead).
"""

from .models import AttrRef, FileInfo, ImportBinding, NameRef, Symbol


def _scope_chain(scope: str) -> list[str]:
    parts = scope.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


class _Resolver:
    def __init__(self, files: dict[str, FileInfo]):
        self.files = files
        self.modules: dict[str, FileInfo] = {fi.module: fi for fi in files.values() if fi.module}
        self.strong: dict[int, int] = {}
        self.weak: dict[int, int] = {}
        self.by_scope_name: dict[tuple[str, str], list[Symbol]] = {}
        self.by_module_symbol: dict[tuple[str, str], list[Symbol]] = {}
        self.by_name: dict[str, list[Symbol]] = {}
        self._weak_names: set[str] = set()
        for fi in files.values():
            for symbol in fi.symbols:
                self.by_scope_name.setdefault((symbol.scope, symbol.name), []).append(symbol)
                if symbol.scope == fi.module:
                    self.by_module_symbol.setdefault((fi.module, symbol.name), []).append(symbol)
                self.by_name.setdefault(symbol.name, []).append(symbol)

    def _add_strong(self, symbol: Symbol) -> None:
        self.strong[symbol.sid] = self.strong.get(symbol.sid, 0) + 1

    def _add_weak(self, symbol: Symbol) -> None:
        self.weak[symbol.sid] = self.weak.get(symbol.sid, 0) + 1

    def _lookup(self, name: str, scope: str) -> list[Symbol]:
        for candidate_scope in _scope_chain(scope):
            found = self.by_scope_name.get((candidate_scope, name))
            if found:
                return found
        return []

    def _finalize_binding(self, binding: ImportBinding) -> None:
        """Turn `from pkg import mod` into a module binding when mod is a module."""
        if binding.symbol is None:
            return
        # `from . import name` binds a submodule of the current package, whose
        # absolute module may be empty when scanning a package directory directly.
        submodule = binding.symbol if not binding.module else f"{binding.module}.{binding.symbol}"
        if submodule in self.modules:
            binding.module = submodule
            binding.symbol = None

    def _follow_binding(self, symbol: Symbol, seen: set[int]) -> None:
        """Mark an import as used and propagate the reference to its target."""
        if symbol.sid in seen:
            return
        seen.add(symbol.sid)
        self._add_strong(symbol)
        binding = symbol.binding
        if binding is None:
            return
        self._finalize_binding(binding)
        if binding.symbol is None:
            return  # plain module import: nothing further to mark
        for target in self.by_module_symbol.get((binding.module, binding.symbol), []):
            if target.kind == "import":
                self._follow_binding(target, seen)
            else:
                self._add_strong(target)

    def _resolve_name(self, file_info: FileInfo, ref: NameRef) -> None:
        symbols = self._lookup(ref.name, ref.scope)
        if not symbols:
            return  # builtin, undefined, or external: ignore
        for symbol in symbols:
            if symbol.kind == "import":
                if ref.is_store:
                    self._add_weak(symbol)
                else:
                    self._follow_binding(symbol, set())
            elif ref.is_store:
                # assignment to an outer-scope name: a mutation, not proof of read
                self._add_weak(symbol)
            else:
                self._add_strong(symbol)

    def _resolve_dotted(self, file_info: FileInfo, dotted: str) -> tuple[str, str | None] | None:
        """Resolve 'a.b.c' where 'a' is a module import. Returns (module, symbol|None)."""
        parts = dotted.split(".")
        binding = file_info.module_imports.get(parts[0])
        if binding is None or binding.symbol is not None:
            return None
        self._finalize_binding(binding)
        module, rest = binding.module, parts[1:]
        index = 0
        while index < len(rest) and f"{module}.{rest[index]}" in self.modules:
            module = f"{module}.{rest[index]}"
            index += 1
        rest = rest[index:]
        if len(rest) == 0:
            return (module, None)
        if len(rest) == 1:
            return (module, rest[0])
        return None

    def _lookup_class(self, file_info: FileInfo, name: str, scope: str) -> list[Symbol]:
        """Find a class by name, following imports for cross-module references."""
        found = [s for s in self._lookup(name, scope) if s.kind == "class"]
        if found:
            return found
        binding = file_info.module_imports.get(name)
        if binding is None:
            return []
        self._finalize_binding(binding)
        if binding.symbol is None:
            return []
        return [
            s
            for s in self.by_module_symbol.get((binding.module, binding.symbol), [])
            if s.kind == "class"
        ]

    def _resolve_via_class(self, file_info: FileInfo, ref: AttrRef) -> bool:
        """Resolve self.x / cls.x to a method of the enclosing class."""
        for scope in _scope_chain(ref.scope):
            if file_info.scope_kinds.get(scope) == "class":
                targets = self.by_scope_name.get((scope, ref.attr), [])
                for target in targets:
                    self._add_strong(target)
                return True
        return False

    def _resolve_via_var_type(self, file_info: FileInfo, ref: AttrRef) -> bool:
        """Resolve h.x where h was assigned as `h = Klass()` in an enclosing scope."""
        for scope in _scope_chain(ref.scope):
            class_name = file_info.var_types.get((scope, ref.base or ""))
            if class_name is None:
                continue
            for class_symbol in self._lookup_class(file_info, class_name, ref.scope):
                targets = self.by_scope_name.get((class_symbol.qualname, ref.attr), [])
                for target in targets:
                    self._add_strong(target)
                return True
            return False
        return False

    def _has_var_type(self, file_info: FileInfo, ref) -> bool:
        return any(
            (scope, ref.base or "") in file_info.var_types for scope in _scope_chain(ref.scope)
        )

    def _is_opaque_base(self, file_info: FileInfo, ref) -> bool:
        """True if the base is a parameter annotated with an external type.

        The annotation is trusted: `node.lineno` with `node: ast.AST` can never
        reference one of our own symbols, so no weak reference is created.
        """
        return any(
            (scope, ref.base or "") in file_info.opaque_bases
            for scope in _scope_chain(ref.scope)
        )

    def _resolve_attr(self, file_info: FileInfo, ref: AttrRef) -> None:
        if ref.dotted:
            resolved = self._resolve_dotted(file_info, ref.dotted)
            if resolved is not None:
                module, symbol_name = resolved
                if symbol_name is not None:
                    for target in self.by_module_symbol.get((module, symbol_name), []):
                        self._add_strong(target)
                return
        if ref.base in ("self", "cls"):
            if self._resolve_via_class(file_info, ref):
                return
        elif ref.base is not None:
            if self._resolve_via_var_type(file_info, ref):
                return
        elif ref.call_base is not None:
            if self._resolve_via_call_base(file_info, ref):
                return
        if ref.base is not None and not self._has_var_type(file_info, ref):
            if self._is_opaque_base(file_info, ref):
                return  # trusted external annotation: cannot reference our symbols
        self._weak_names.add(ref.attr)

    def _resolve_via_call_base(self, file_info: FileInfo, ref: AttrRef) -> bool:
        """Resolve `Klass(...).method()` to a method of Klass."""
        for class_symbol in self._lookup_class(file_info, ref.call_base or "", ref.scope):
            targets = self.by_scope_name.get((class_symbol.qualname, ref.attr), [])
            for target in targets:
                self._add_strong(target)
            return True
        return False

    def _apply_weak_names(self) -> None:
        for name in self._weak_names:
            for symbol in self.by_name.get(name, []):
                if symbol.kind in ("function", "method", "variable"):
                    self._add_weak(symbol)

    def _apply_star_imports(self) -> None:
        for file_info in self.files.values():
            for module_name in file_info.star_imports:
                target = self.modules.get(module_name)
                if target is None:
                    continue
                if target.all_exports:
                    names = set(target.all_exports)
                else:
                    names = {
                        s.name
                        for s in target.symbols
                        if s.scope == target.module and not s.name.startswith("_")
                    }
                for symbol in target.symbols:
                    if symbol.scope == target.module and symbol.name in names:
                        self._add_weak(symbol)

    def _apply_dynamic_names(self) -> None:
        for file_info in self.files.values():
            for name in file_info.dynamic_names:
                for symbol in self.by_name.get(name, []):
                    if symbol.kind in ("function", "method", "variable"):
                        self._add_weak(symbol)

    def _apply_init_reexports(self) -> None:
        """In __init__.py, from-imports are conventionally public re-exports."""
        for file_info in self.files.values():
            if not file_info.is_init:
                continue
            for symbol in file_info.symbols:
                if (
                    symbol.kind == "import"
                    and symbol.binding is not None
                    and symbol.binding.symbol is not None
                ):
                    self._follow_binding(symbol, set())

    def run(self) -> tuple[dict[int, int], dict[int, int]]:
        for file_info in self.files.values():
            for ref in file_info.name_refs:
                self._resolve_name(file_info, ref)
            for ref in file_info.attr_refs:
                self._resolve_attr(file_info, ref)
        self._apply_init_reexports()
        self._apply_weak_names()
        self._apply_star_imports()
        self._apply_dynamic_names()
        return self.strong, self.weak


def resolve_references(files: dict[str, FileInfo]) -> tuple[dict[int, int], dict[int, int]]:
    """Resolve all references; return (strong_refs, weak_refs) keyed by symbol id."""
    return _Resolver(files).run()
