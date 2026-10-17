"""AST-based scanner: extracts symbol definitions and references from Python files.

Only the standard library is used (``ast``). Every handler method is invoked
through ``self``, so the scanner's own code stays analyzable (dogfood-safe).
"""

import ast
import fnmatch
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

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".tox",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
    }
)
_TEST_DIR_NAMES = frozenset({"tests", "test"})

_DYNAMIC_CALLS = frozenset({"eval", "exec", "compile", "__import__", "globals", "locals", "vars"})


def is_test_file(rel: Path) -> bool:
    """Heuristic: is this a test file?"""
    name = rel.name
    if name == "conftest.py":
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(part in _TEST_DIR_NAMES for part in rel.parts[:-1])


def _load_gitignore(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.is_file():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line)
    return patterns


def _gitignored(rel_posix: str, patterns: list[str]) -> bool:
    parts = rel_posix.split("/")
    for pat in patterns:
        pat = pat.strip("/")
        if not pat:
            continue
        if "/" not in pat:
            if any(fnmatch.fnmatch(part, pat) for part in parts):
                return True
        elif fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(rel_posix, pat + "/*"):
            return True
    return False


def iter_python_files(root: Path) -> list[Path]:
    """Recursively find .py files, skipping venvs, caches and gitignored paths."""
    patterns = _load_gitignore(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP_DIRS
            and not _gitignored(
                (rel_dir + "/" + d).lstrip("./") if rel_dir != "." else d, patterns
            )
        ]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            full = Path(dirpath) / filename
            rel = full.relative_to(root).as_posix()
            if _gitignored(rel, patterns):
                continue
            found.append(full)
    return sorted(found)


def module_name_for(root: Path, path: Path) -> tuple[str, bool]:
    """Return (dotted module name, is_package_init) for a file."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    is_init = bool(parts) and parts[-1] == "__init__"
    if is_init:
        parts = parts[:-1]
    return (".".join(parts), is_init)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and len(name) > 4


def _is_test_entry(name: str, decorators: list[str], in_test_file: bool) -> bool:
    if not in_test_file:
        return False
    if any(d.split(".")[-1] == "fixture" for d in decorators):
        return True
    return name == "test" or name.startswith("test_")


class _ModuleScanner:
    """Extracts definitions and references from a single module."""

    def __init__(self, file_info: FileInfo, sid_counter: list[int]):
        self.fi = file_info
        # scope stack of (kind, qualname); kind is module|class|function
        self._scope: list[tuple[str, str]] = [("module", file_info.module)]
        self._main_depth = 0
        self._sid_counter = sid_counter
        file_info.scope_kinds[file_info.module] = "module"
        self._dispatch = {
            "FunctionDef": self._functiondef,
            "AsyncFunctionDef": self._functiondef,
            "ClassDef": self._classdef,
            "Import": self._import,
            "ImportFrom": self._importfrom,
            "Name": self._name,
            "Attribute": self._attribute,
            "Assign": self._assign,
            "AnnAssign": self._annassign,
            "AugAssign": self._augassign,
            "If": self._if,
            "Call": self._call,
            "Lambda": self._lambda,
        }

    # -- helpers ---------------------------------------------------------

    @property
    def _scope_qualname(self) -> str:
        return self._scope[-1][1]

    @property
    def _scope_kind(self) -> str:
        return self._scope[-1][0]

    def _new_sid(self) -> int:
        self._sid_counter[0] += 1
        return self._sid_counter[0]

    def _add_symbol(self, symbol: Symbol) -> None:
        self.fi.symbols.append(symbol)

    def _make_symbol(self, name: str, kind: str, node: ast.AST) -> Symbol:
        qualname = f"{self._scope_qualname}.{name}" if self._scope_qualname else name
        return Symbol(
            sid=self._new_sid(),
            name=name,
            kind=kind,
            qualname=qualname,
            scope=self._scope_qualname,
            file=self.fi.path,
            lineno=node.lineno,
            end_lineno=getattr(node, "end_lineno", node.lineno) or node.lineno,
            in_main_block=self._main_depth > 0,
            in_test_file=self.fi.is_test,
        )

    @staticmethod
    def _dotted(node: ast.AST) -> str:
        """Best-effort dotted name for a decorator or base expression."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            inner = _ModuleScanner._dotted(node.value)
            return f"{inner}.{node.attr}" if inner else node.attr
        if isinstance(node, ast.Call):
            return _ModuleScanner._dotted(node.func)
        if isinstance(node, ast.Subscript):
            return _ModuleScanner._dotted(node.value)
        return ""

    def _dotted_path(self, node: ast.AST) -> str | None:
        """Full dotted path if the expression is only Names and Attributes."""
        parts: list[str] = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
        return None

    def _abs_module(self, level: int, module: str | None) -> str:
        current = self.fi.module
        if self.fi.is_init:
            package = current
        else:
            package = current.rpartition(".")[0]
        if level == 0:
            return module or ""
        parts = package.split(".") if package else []
        drop = level - 1
        base = ".".join(parts[: len(parts) - drop]) if drop <= len(parts) else ""
        if module:
            return f"{base}.{module}" if base else module
        return base

    @staticmethod
    def _annotation_class_name(annotation: ast.AST | None) -> str | None:
        """Simple class name if the annotation is a bare Name."""
        if isinstance(annotation, ast.Name):
            return annotation.id
        return None

    @staticmethod
    def _is_external_annotation(annotation: ast.AST | None) -> bool:
        """True if the annotation references an external qualified type (e.g. ast.X)."""
        if annotation is None:
            return False
        return any(isinstance(child, ast.Attribute) for child in ast.walk(annotation))

    @staticmethod
    def _const_str_list(node: ast.AST) -> list[str]:
        if isinstance(node, (ast.List, ast.Tuple)):
            return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return []

    @staticmethod
    def _is_main_check(test: ast.AST) -> bool:
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            return False
        if not isinstance(test.ops[0], ast.Eq) or len(test.comparators) != 1:
            return False
        left, right = test.left, test.comparators[0]
        pairs = [(left, right), (right, left)]
        return any(
            isinstance(a, ast.Name)
            and a.id == "__name__"
            and isinstance(b, ast.Constant)
            and b.value == "__main__"
            for a, b in pairs
        )

    def _target_names(self, target: ast.AST) -> list[str]:
        """Store-target names (for variable definitions)."""
        if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store):
            return [target.id]
        if isinstance(target, ast.Starred):
            return self._target_names(target.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            names: list[str] = []
            for elt in target.elts:
                names.extend(self._target_names(elt))
            return names
        return []

    def _visit_store_value(self, target: ast.AST) -> None:
        """Visit the object part of complex store targets for references."""
        if isinstance(target, ast.Attribute):
            self._visit(target.value)
        elif isinstance(target, ast.Subscript):
            self._visit(target.value)
            self._visit(target.slice)
        elif isinstance(target, ast.Starred):
            self._visit_store_value(target.value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._visit_store_value(elt)

    def _visit_annotation(self, annotation: ast.AST) -> None:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                tree = ast.parse(annotation.value, mode="eval")
            except SyntaxError:
                return
            for child in ast.walk(tree):
                if isinstance(child, ast.Name):
                    self.fi.name_refs.append(
                        NameRef(child.id, self._scope_qualname, annotation.lineno)
                    )
        else:
            self._visit(annotation)

    # -- dispatch ----------------------------------------------------------

    def run(self, tree: ast.Module) -> None:
        for stmt in tree.body:
            self._visit(stmt)

    def _visit(self, node: ast.AST) -> None:
        handler = self._dispatch.get(type(node).__name__)
        if handler is not None:
            handler(node)
        else:
            for child in ast.iter_child_nodes(node):
                self._visit(child)

    # -- definitions --------------------------------------------------------

    def _functiondef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = METHOD if self._scope_kind == "class" else FUNCTION
        decorators = [self._dotted(d) for d in node.decorator_list]
        decorators = [d for d in decorators if d]
        sym = self._make_symbol(node.name, kind, node)
        sym.decorators = decorators
        sym.is_dunder = _is_dunder(node.name)
        sym.is_abstract = any(d == "abstractmethod" or d.endswith(".abstractmethod") for d in decorators)
        sym.is_entry_point = _is_test_entry(node.name, decorators, self.fi.is_test)
        self._add_symbol(sym)
        # decorators, defaults and annotations live in the enclosing scope
        for dec in node.decorator_list:
            self._visit(dec)
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
            self._visit(default)
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            if arg.annotation is not None:
                self._visit_annotation(arg.annotation)
        if args.vararg is not None and args.vararg.annotation is not None:
            self._visit_annotation(args.vararg.annotation)
        if args.kwarg is not None and args.kwarg.annotation is not None:
            self._visit_annotation(args.kwarg.annotation)
        if node.returns is not None:
            self._visit_annotation(node.returns)
        self._scope.append(("function", sym.qualname))
        self.fi.scope_kinds[sym.qualname] = "function"
        # parameter annotations double as poor-man's type info for `param.attr`;
        # annotations pointing at external types mark the param as opaque.
        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            ann_name = self._annotation_class_name(arg.annotation)
            if ann_name:
                self.fi.var_types[(sym.qualname, arg.arg)] = ann_name
            elif self._is_external_annotation(arg.annotation):
                self.fi.opaque_bases.add((sym.qualname, arg.arg))
        for stmt in node.body:
            self._visit(stmt)
        self._scope.pop()

    def _classdef(self, node: ast.ClassDef) -> None:
        decorators = [self._dotted(d) for d in node.decorator_list]
        decorators = [d for d in decorators if d]
        sym = self._make_symbol(node.name, CLASS, node)
        sym.decorators = decorators
        sym.is_dunder = _is_dunder(node.name)
        self._add_symbol(sym)
        for dec in node.decorator_list:
            self._visit(dec)
        for base in node.bases:
            self._visit(base)
        for keyword in node.keywords:
            self._visit(keyword.value)
        self._scope.append(("class", sym.qualname))
        self.fi.scope_kinds[sym.qualname] = "class"
        for stmt in node.body:
            self._visit(stmt)
        self._scope.pop()

    def _import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                local, module = alias.asname, alias.name
            else:
                local, module = alias.name.split(".")[0], alias.name.split(".")[0]
            binding = ImportBinding(module=module, symbol=None, lineno=node.lineno)
            sym = self._make_symbol(local, IMPORT, node)
            sym.binding = binding
            self._add_symbol(sym)
            if self._scope_kind == "module":
                self.fi.module_imports[local] = binding

    def _importfrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "*" for alias in node.names):
            self.fi.star_imports.append(self._abs_module(node.level, node.module))
            return
        if node.module == "__future__":
            return  # compiler directive, never "used"
        abs_module = self._abs_module(node.level, node.module or "")
        for alias in node.names:
            local = alias.asname or alias.name
            binding = ImportBinding(
                module=abs_module,
                symbol=alias.name,
                lineno=node.lineno,
            )
            sym = self._make_symbol(local, IMPORT, node)
            sym.binding = binding
            self._add_symbol(sym)
            if self._scope_kind == "module":
                self.fi.module_imports[local] = binding

    def _assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                self.fi.all_exports.update(self._const_str_list(node.value))
        if self._scope_kind in ("module", "class"):
            for target in node.targets:
                for name in self._target_names(target):
                    sym = self._make_symbol(name, VARIABLE, target)
                    sym.is_dunder = _is_dunder(name)
                    self._add_symbol(sym)
        else:
            # store inside a function: may mutate a module-level name -> weak ref
            for target in node.targets:
                for name in self._target_names(target):
                    self.fi.name_refs.append(
                        NameRef(name, self._scope_qualname, node.lineno, is_store=True)
                    )
        self._visit(node.value)
        for target in node.targets:
            self._visit_store_value(target)
        # record simple `var = ClassName()` for instance-method resolution
        if (
            self._scope_kind in ("module", "function")
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.targets[0].ctx, ast.Store)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            self.fi.var_types[(self._scope_qualname, node.targets[0].id)] = node.value.func.id

    def _annassign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.target.id == "__all__" and node.value:
            self.fi.all_exports.update(self._const_str_list(node.value))
        self._visit_annotation(node.annotation)
        if node.value is not None:
            self._visit(node.value)
        target = node.target
        if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store):
            if self._scope_kind in ("module", "class"):
                sym = self._make_symbol(target.id, VARIABLE, target)
                sym.is_dunder = _is_dunder(target.id)
                self._add_symbol(sym)
            else:
                self.fi.name_refs.append(
                    NameRef(target.id, self._scope_qualname, node.lineno, is_store=True)
                )
            ann_name = self._annotation_class_name(node.annotation)
            if ann_name:
                self.fi.var_types[(self._scope_qualname, target.id)] = ann_name
            if (
                self._scope_kind in ("module", "function")
                and node.value is not None
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
            ):
                self.fi.var_types[(self._scope_qualname, target.id)] = node.value.func.id
        else:
            self._visit_store_value(target)

    def _augassign(self, node: ast.AugAssign) -> None:
        target = node.target
        if isinstance(target, ast.Name):
            self.fi.name_refs.append(NameRef(target.id, self._scope_qualname, node.lineno))
        elif isinstance(target, ast.Attribute):
            self._attribute(target, force_load=True)
        self._visit(node.value)

    def _if(self, node: ast.If) -> None:
        if self._is_main_check(node.test):
            self._main_depth += 1
            for stmt in node.body:
                self._visit(stmt)
            self._main_depth -= 1
            for stmt in node.orelse:
                self._visit(stmt)
        else:
            self._visit(node.test)
            for stmt in node.body:
                self._visit(stmt)
            for stmt in node.orelse:
                self._visit(stmt)

    def _lambda(self, node: ast.Lambda) -> None:
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
            self._visit(default)
        qualname = f"{self._scope_qualname}.<lambda>"
        self._scope.append(("function", qualname))
        self._visit(node.body)
        self._scope.pop()

    # -- references ----------------------------------------------------------

    def _name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Load, ast.Del)):
            self.fi.name_refs.append(NameRef(node.id, self._scope_qualname, node.lineno))

    def _attribute(self, node: ast.Attribute, force_load: bool = False) -> None:
        if isinstance(node.ctx, ast.Load) or force_load:
            base = node.value.id if isinstance(node.value, ast.Name) else None
            call_base = None
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                call_base = node.value.func.id
            self.fi.attr_refs.append(
                AttrRef(
                    node.attr,
                    base,
                    self._dotted_path(node),
                    call_base,
                    self._scope_qualname,
                    node.lineno,
                )
            )
            if base is not None:
                # the base itself is read, e.g. `worker` in `worker.run()`
                self.fi.name_refs.append(NameRef(base, self._scope_qualname, node.lineno))
        self._visit(node.value)

    def _call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _DYNAMIC_CALLS:
                self.fi.dynamic_scopes.add(self._scope_qualname)
            elif func.id in ("getattr", "hasattr"):
                self.fi.dynamic_scopes.add(self._scope_qualname)
                if len(node.args) >= 2:
                    name_arg = node.args[1]
                    if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
                        self.fi.dynamic_names.add(name_arg.value)
        for child in ast.iter_child_nodes(node):
            self._visit(child)


def scan_file(root: Path, path: Path, sid_counter: list[int]) -> FileInfo | None:
    """Parse one file and extract its symbols and references."""
    rel = path.relative_to(root).as_posix()
    module, is_init = module_name_for(root, path)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, ValueError, OSError):
        return None
    file_info = FileInfo(
        path=rel,
        module=module,
        is_init=is_init,
        is_test=is_test_file(path.relative_to(root)),
    )
    _ModuleScanner(file_info, sid_counter).run(tree)
    # __all__ exports mark matching module-level symbols
    for symbol in file_info.symbols:
        if symbol.scope == module and symbol.name in file_info.all_exports:
            symbol.exported = True
    return file_info


def scan_directory(root: Path | str) -> dict[str, FileInfo]:
    """Scan a directory (or a single file) and return per-file extraction results."""
    root_path = Path(root)
    if root_path.is_file():
        paths = [root_path]
        root_path = root_path.parent
    else:
        paths = iter_python_files(root_path)
    sid_counter = [0]
    files: dict[str, FileInfo] = {}
    for path in paths:
        info = scan_file(root_path, path, sid_counter)
        if info is not None:
            files[info.path] = info
    return files
