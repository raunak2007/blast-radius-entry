"""Decide which tests one changed file can affect, using coverage and fixture maps.

Precise path: a changed statement inside a function or lambda selects the tests that
executed that statement. Anything evaluated at import time (class or module level,
new functions) falls back to the tests that touched the class, module, or dialect
family. Unknowns select everything (ALL).
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field

from br.astutil import chain as _chain
from br.astutil import own_lines as _own_lines
from br.astutil import start as _start
from br.diff import FileDiff, Hunk, apply_hunks
from br.equiv import Equivalence, module_key
from br.readers import ReaderIndex

ALL = None  # sentinel meaning "every test"


@dataclass
class RefineInfo:
    """What one file's import-time edit needs once the import check reports which
    import-time state changed: the selection of its in-function lines (run-time
    coverage only), the names its class/module-level assignments bind, and whether
    every import-time line was a plain assignment or function code (else no refining)."""

    runtime: set[int] = field(default_factory=set)
    keys: set[tuple[tuple[str, ...], str]] = field(default_factory=set)
    functions: set[tuple[str, ...]] = field(default_factory=set)  # edited module/class-level functions
    refinable: bool = True
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
NAMED_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
DOC_SUFFIXES = (".md", ".rst", ".txt", ".png", ".svg", ".html")
FAMILY_DIRS = ("dialects", "parsers", "generators", "typing")
FAMILY_SKIP = ("__init__", "dialect", "base")
PACKAGE_WALKERS = {"sqlglot/typing/": "tests/test_typing.py"}
RUNTIME_IMPORTERS = ("tests/test_dialect_imports.py",)  # test files importing sqlglot modules inside tests
WHOLE_FILE = (0, 10**9)


@dataclass
class Maps:
    coverage: dict[str, dict[int, list[int]]]
    fixtures: dict[str, list[int]]
    tests_by_file: dict[str, set[int]]
    classes: dict[str, list[int]] = field(default_factory=dict)
    enums: frozenset[str] = frozenset()
    import_lines: dict[str, list[int]] = field(default_factory=dict)
    collect_imports: dict[str, list[str]] = field(default_factory=dict)  # module path -> test files importing it
    import_groups: dict = field(default_factory=dict)  # {"lines": {file: {line: [gid]}}, "groups": {gid: tests|"ALL"}}


ASSIGN_TYPES = (ast.Assign, ast.AnnAssign, ast.AugAssign)


def _assigned_names(stmt: ast.AST) -> list[str] | None:
    """Plain names a class/module-level assignment binds, or None for anything else."""
    if not isinstance(stmt, ASSIGN_TYPES):
        return None
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    names = []
    for target in targets:
        if not isinstance(target, ast.Name):
            return None
        names.append(target.id)
    return names or None




ENUM_BASES = frozenset({"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "AutoName"})
FIELD_BASES = frozenset({"NamedTuple", "TypedDict"})


def _base_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _is_enum(cls: ast.ClassDef, enums: frozenset[str]) -> bool:
    return bool({_base_name(b) for b in cls.bases} & (ENUM_BASES | set(enums)))


def _has_generic_fields(cls: ast.ClassDef, enums: frozenset[str]) -> bool:
    """Dataclasses, enums and named tuples read their class-level names generically
    (generated __init__/__eq__/__repr__, member iteration and value lookup), so a
    by-name reader search cannot bound who sees a change."""
    if any("dataclass" in _base_name(d) for d in cls.decorator_list):
        return True
    bases = {_base_name(b) for b in cls.bases}
    return bool(bases & (ENUM_BASES | FIELD_BASES | set(enums)))


def _module_name(path: str) -> str:
    name = path[:-3].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def _qualname(chain: list[ast.AST]) -> tuple[str, ...]:
    return tuple(n.name for n in chain if isinstance(n, SCOPE_TYPES))


def _scopes(tree: ast.AST) -> dict[tuple[str, ...], ast.AST]:
    out: dict[tuple[str, ...], ast.AST] = {}

    def walk(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, SCOPE_TYPES):
                name = prefix + (child.name,)
                out.setdefault(name, child)
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, ())
    return out


def _code_line(lines: list[str], idx: int, step: int) -> int | None:
    """Nearest 1-based line from idx (inclusive) in direction `step` that holds code."""
    while 1 <= idx <= len(lines):
        text = lines[idx - 1].strip()
        if text and not text.startswith("#"):
            return idx
        idx += step
    return None


def _is_code(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("#")


def _union(parts) -> set[int] | None:
    out: set[int] = set()
    for part in parts:
        if part is ALL:
            return ALL
        out |= part
    return out


def _added(hunk: Hunk) -> list[tuple[int, int]]:
    """(old insertion point, new line number) for every '+' line, single-hunk application."""
    out = []
    old_cursor = hunk.old_start if hunk.old_len else hunk.old_start + 1
    new_cursor = old_cursor
    for line in hunk.lines:
        if line.startswith("+"):
            out.append((old_cursor, new_cursor))
            new_cursor += 1
        elif line.startswith("-"):
            old_cursor += 1
        else:
            old_cursor += 1
            new_cursor += 1
    return out


class Analyzer:
    def __init__(self, src_root: str, maps: Maps) -> None:
        self.src_root = src_root
        self.maps = maps
        self.readers = ReaderIndex(src_root, maps.import_lines)
        self.equiv = Equivalence(maps.enums)
        self._single = None  # (tree, key) of the patched file when it has one hunk
        self.import_time: dict[str, str] = {}  # path -> patched source, for the import check
        self._touched_import_time = False
        self.refine_info: dict[str, RefineInfo] = {}
        self._info = RefineInfo()
        # for the TPC-DS probe: changed (path, start, end) when every edit is function
        # code, else None; and the patched text of each Python file
        self.changed_ranges: list[tuple[str, int, int]] | None = []
        self.patched_sources: dict[str, str] = {}
        self.verifiable = False

    def reset(self) -> None:
        self.verifiable = False  # set by the selector: may the selection be run here?
        self.import_time = {}
        self.refine_info = {}
        self.changed_ranges = []
        self.patched_sources = {}

    def _changed(self, path: str, start: int, end: int) -> None:
        if self.changed_ranges is not None:
            self.changed_ranges.append((path, start, end))

    def _unprobeable(self) -> None:
        self.changed_ranges = None

    def _import_effect(self, path: str, start: int, end: int) -> set[int] | None:
        """Tests that depend on lines start..end through what they build at import time."""
        lines = self.maps.import_groups.get("lines", {}).get(path, {})
        groups = self.maps.import_groups.get("groups", {})
        tests: set[int] = set()
        for line in range(start, end + 1):
            for gid in lines.get(str(line), ()):
                group = groups.get(str(gid))
                if group == "ALL":
                    return ALL
                tests.update(group or ())
        return tests

    def _mark_import_time(self, path: str, start: int, end: int) -> None:
        lines = self.maps.import_lines.get(path, ())
        if any(start <= line <= end for line in lines):
            self._touched_import_time = True

    def _narrow(self, path: str, stmt: ast.AST | None, defining, broad: set[int] | None):
        """Intersect a broad selection with the tests that read the assigned names."""
        names = _assigned_names(stmt) if stmt is not None else None
        if broad is ALL:
            return ALL
        # Import-time state derived from a class/module statement is only used by that
        # class's users, which `broad` already holds; crashes are caught by the import check.
        unbounded = broad
        if not names or not self.maps.import_lines:
            return unbounded
        readers: set[int] = set()
        for name in names:
            sites = self.readers.reader_lines(name, defining)
            if not sites:
                return unbounded  # None: unbounded reads; []: read only by library code, if at all
            for site_path, start, end in sites:
                readers |= self._range(site_path, start, end)
        return broad & readers

    # ---- refining import-time edits with the import check's state diff ----
    def refine(self, changed: dict[str, str] | None) -> set[int] | None:
        """Selection for the files in refine_info, given every import-time state key whose
        value the patch changed. None means "keep the conservative selection".

        Import-time code can only influence a test through state it leaves behind. When
        that state is plain data, a test can only observe it by running a statement that
        reads the attribute: the class's broad set intersected with those readers.
        """
        if changed is None or not self.refine_info:
            return None
        edited = {
            f"{_module_name(path)}::{'.'.join(qual)}" for path, info in self.refine_info.items() for qual in info.functions
        }
        # an edited function's own key changes with its code: its run-time coverage is
        # already in `runtime`; any other function, method or class change is unknown
        if any(kind != "data" and key not in edited for key, kind in changed.items()):
            return None
        keys = {key for key, kind in changed.items() if kind == "data"}
        tests: set[int] = set()
        for path, info in self.refine_info.items():
            if not info.refinable:
                return None
            module = _module_name(path)
            keys |= {f"{module}::{'.'.join(qual + (name,))}" for qual, name in info.keys}
            tests |= info.runtime
        for key in sorted(keys):
            part = self._key_tests(key)
            if part is ALL:
                return None
            tests |= part
        return tests

    def crash_tests(self, failed: list[str]) -> set[int] | None:
        """Tests that can notice modules failing to import (None: unknown, select all).

        A module that raises while importing is never usable, so only tests that load it
        see the patch: those whose test file imports it at collection, those that ran any
        of its code or constructed one of its classes (lazily loaded dialects included),
        and those that walk every dialect (Dialect.classes, sqlglot.dialects.__getattr__)."""
        tests: set[int] = set()
        walkers = False
        for name in failed:
            path = self._module_path(name)
            if path is None:
                return ALL
            if path.startswith("tests/"):
                if path not in self.maps.tests_by_file and not path.endswith("__init__.py"):
                    return ALL  # a helper module of the tests (tests.helpers, ...)
                tests |= self.maps.tests_by_file.get(path, set())
                continue
            module = self._module(path)
            if module is ALL:
                return ALL
            tests |= module
            prefix = path + "::"
            for key, users in self.maps.classes.items():
                if key.startswith(prefix):
                    tests.update(users)
            for test_file in self.maps.collect_imports.get(path, ()):
                tests |= self.maps.tests_by_file.get(test_file, set())
            walkers = walkers or path.split("/")[1] in FAMILY_DIRS
        if walkers:
            tests |= self._range("sqlglot/dialects/dialect.py", *self._classes_property())
            tests |= self._range("sqlglot/dialects/__init__.py", *WHOLE_FILE)
        names = {n for n in failed if not n.startswith("tests")}
        # imports inside function bodies run whenever the function does
        for site_path, start, end in self._runtime_imports():
            if names & self._runtime_imports()[(site_path, start, end)]:
                tests |= self._range(site_path, start, end)
        for name in names:
            parts = name.split(".")
            for depth in range(1, len(parts)):  # a package's lazy __getattr__ imports submodules
                package = self._module_path(".".join(parts[:depth]))
                if package and package.endswith("__init__.py"):
                    for node in self.readers.tree(package).body:
                        if isinstance(node, NAMED_FUNCS) and node.name == "__getattr__":
                            tests |= self._range(package, _start(node), node.end_lineno)
            path = self._module_path(name) or ""
            for prefix, test_file in PACKAGE_WALKERS.items():
                if path.startswith(prefix):
                    tests |= self.maps.tests_by_file.get(test_file, set())
        for test_file in RUNTIME_IMPORTERS:
            tests |= self.maps.tests_by_file.get(test_file, set())
        return tests

    def _runtime_imports(self) -> dict[tuple[str, int, int], set[str]]:
        """(path, start, end) of every import statement inside a function in sqlglot/, with
        the module names it can load."""
        if getattr(self, "_rt_imports", None) is None:
            out: dict[tuple[str, int, int], set[str]] = {}
            for path in self.readers._all_texts():
                if not path.startswith("sqlglot/"):
                    continue
                package = _module_name(path) if path.endswith("__init__.py") else _module_name(path).rpartition(".")[0]

                def visit(node: ast.AST, in_func: bool) -> None:
                    for child in ast.iter_child_nodes(node):
                        if in_func and isinstance(child, (ast.Import, ast.ImportFrom)):
                            mods: set[str] = set()
                            if isinstance(child, ast.Import):
                                for alias in child.names:
                                    parts = alias.name.split(".")
                                    mods |= {".".join(parts[:i]) for i in range(1, len(parts) + 1)}
                            else:
                                base = child.module or ""
                                if child.level:
                                    pkg = package.split(".")
                                    pkg = pkg[: len(pkg) - child.level + 1]
                                    base = ".".join(pkg + ([base] if base else []))
                                parts = base.split(".")
                                mods |= {".".join(parts[:i]) for i in range(1, len(parts) + 1)}
                                mods |= {f"{base}.{alias.name}" for alias in child.names}
                            out[(path, child.lineno, child.end_lineno)] = mods
                        visit(child, in_func or isinstance(child, FUNC_TYPES))

                try:
                    visit(self.readers.tree(path), False)
                except SyntaxError:
                    continue
            self._rt_imports = out
        return self._rt_imports

    def _classes_property(self) -> tuple[int, int]:
        """Lines of Dialect.classes and Dialect._try_load (they load every dialect module)."""
        tree = self.readers.tree("sqlglot/dialects/dialect.py")
        lines = [(_start(n), n.end_lineno) for n in ast.walk(tree)
                 if isinstance(n, NAMED_FUNCS) and n.name in ("classes", "_try_load")]
        if not lines:
            return WHOLE_FILE
        return min(a for a, _ in lines), max(b for _, b in lines)

    def _key_tests(self, key: str) -> set[int] | None:
        module, _, qual = key.partition("::")
        path = self._module_path(module)
        if path is None:
            return ALL
        parts = qual.split(".")
        name = parts[-1]
        if name.startswith("__") and name.endswith("__"):
            return ALL  # __annotations__, __init_subclass__ and other protocol hooks
        readers = self.readers.runtime_reader_lines(name) or None  # no reader in our code: unknown
        if len(parts) == 1:
            # a module global: readers elsewhere may hold it from `from m import NAME`,
            # so the defining module's own coverage is no bound
            return self._module(path) if readers is None else self._read(readers)
        classes = self._class_chain(path, parts[:-1])
        if len(classes) != len(parts) - 1:
            return ALL
        broad = self._broad(path, classes)
        if broad is not ALL:
            for depth in range(1, len(classes)):  # users of enclosing classes (Dialect for Dialect.Tokenizer)
                broad |= self._class_users(path, classes[:depth])
        if readers is None:
            return broad
        return self._read(readers) if broad is ALL else broad & self._read(readers)

    def _read(self, sites) -> set[int]:
        out: set[int] = set()
        for site_path, start, end in sites:
            out |= self._range(site_path, start, end)
        return out

    def _module_path(self, module: str) -> str | None:
        base = module.replace(".", "/")
        for path in (base + ".py", base + "/__init__.py"):
            if os.path.exists(os.path.join(self.src_root, path)):
                return path
        return None

    def _class_chain(self, path: str, names: list[str]) -> list[ast.AST]:
        tree = self.readers.tree(path)
        scopes = _scopes(tree)
        out = []
        for depth in range(1, len(names) + 1):
            node = scopes.get(tuple(names[:depth]))
            if not isinstance(node, ast.ClassDef):
                return out
            out.append(node)
        return out

    # ---- coverage helpers -------------------------------------------------
    def _lines(self, path: str, lines) -> set[int]:
        cov = self.maps.coverage.get(path, {})
        out: set[int] = set()
        for line in lines:
            out.update(cov.get(line, ()))
        return out

    def _range(self, path: str, start: int, end: int) -> set[int]:
        out: set[int] = set()
        for line, tests in self.maps.coverage.get(path, {}).items():
            if start <= line <= end:
                out.update(tests)
        return out

    def _module(self, path: str) -> set[int] | None:
        tests = self._range(path, *WHOLE_FILE) | self.maps.tests_by_file.get(path, set())
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in FAMILY_DIRS:
            name = parts[2][:-3]
            if name not in FAMILY_SKIP:
                for folder in FAMILY_DIRS:
                    tests |= self._range(f"sqlglot/{folder}/{name}.py", *WHOLE_FILE)
                tests |= self.maps.tests_by_file.get(f"tests/dialects/test_{name}.py", set())
        return tests or ALL

    def _broad(self, path: str, classes: list[ast.AST]) -> set[int] | None:
        """Tests that can see an import-time change in `path` (inside the innermost class)."""
        if path.startswith("tests/") and path in self.maps.tests_by_file:
            return set(self.maps.tests_by_file[path])
        used: set[int] = set()
        in_class: set[int] = set()
        if classes:
            used = self._class_users(path, classes)
            in_class = self._range(path, _start(classes[-1]), classes[-1].end_lineno)
        if classes and path.startswith("sqlglot/expressions/") and (used or in_class):
            return used | in_class
        module = self._module(path)
        if module is ALL or not classes:
            return module
        is_family = path.count("/") == 2 and path.split("/")[1] in FAMILY_DIRS
        if is_family:
            return in_class | module | used
        return (in_class | used) if (in_class or used) else module

    def _class_users(self, path: str, classes: list[ast.AST]) -> set[int]:
        """Tests that constructed this class or a subclass (any nesting prefix counts)."""
        names = [c.name for c in classes]
        users: set[int] = set()
        for depth in range(len(names), 0, -1):
            users |= set(self.maps.classes.get(f"{path}::{'.'.join(names[:depth])}", ()))
            if users:
                break
        return users

    # ---- per-line analysis on the old tree --------------------------------
    def _old_line(self, path: str, tree: ast.AST, line: int) -> set[int] | None:
        chain = _chain(tree, line)
        funcs = [i for i, n in enumerate(chain) if isinstance(n, FUNC_TYPES)]
        if not funcs:
            self._touched_import_time = True
            self._unprobeable()
            classes = [n for n in chain if isinstance(n, ast.ClassDef)]
            broad = self._broad(path, classes)
            stmts = [n for n in chain if isinstance(n, ast.stmt)]
            stmt = stmts[-1] if stmts else None
            self._record_keys(chain, stmt, line)
            if classes and _has_generic_fields(classes[-1], self.maps.enums):
                return self._generic_broad(path, classes, broad)
            defining = (path, _start(stmt), stmt.end_lineno) if stmt is not None else (path, 0, -1)
            return self._narrow(path, stmt, defining, broad)
        idx = funcs[-1]
        func = chain[idx]
        self._info.functions.add(_qualname(chain[: funcs[0] + 1]))
        if isinstance(func, ast.Lambda):
            self._changed(path, func.lineno, func.end_lineno)
            tests = self._range(path, func.lineno, func.end_lineno)
            self._info.runtime |= tests
            return tests
        if line < func.body[0].lineno:  # decorator, signature, defaults
            self._touched_import_time = True  # evaluated when the def runs, often at import
            self._info.refinable = False
            self._unprobeable()
            return self._range(path, _start(func), func.end_lineno)
        stmts = [n for n in chain[idx + 1:] if isinstance(n, ast.stmt)]
        if not stmts:
            self._changed(path, line, line)
            tests = self._lines(path, {line})
            self._info.runtime |= tests
            return tests
        start, end = min(_own_lines(stmts[-1])[0], line), max(_own_lines(stmts[-1])[1], line)
        self._mark_import_time(path, start, end)
        self._changed(path, start, end)
        runtime = self._range(path, start, end)
        self._info.runtime |= runtime
        return _union([runtime, self._import_effect(path, start, end)])

    def _generic_broad(self, path: str, classes: list[ast.AST], broad: set[int] | None) -> set[int] | None:
        """An enum member is never constructed: its value reaches whoever uses the enum,
        so add every test that ran code in the defining module."""
        if _is_enum(classes[-1], self.maps.enums):
            return _union([broad, self._module(path)])
        return broad

    def _record_keys(self, chain: list[ast.AST], stmt: ast.AST | None, line: int) -> None:
        """Remember the class/module-level names a statement binds, for refine()."""
        if stmt is None or (isinstance(stmt, ast.ClassDef) and stmt.body and line >= stmt.body[0].lineno):
            return  # a blank or comment line between statements
        names = _assigned_names(stmt)
        if not names:
            self._info.refinable = False
            return
        classes = [n for n in chain if isinstance(n, ast.ClassDef)]
        qual = tuple(n.name for n in classes)
        if classes and _has_generic_fields(classes[-1], self.maps.enums):
            self._info.refinable = False
            return
        if len(qual) != len([n for n in chain if isinstance(n, SCOPE_TYPES)]):
            self._info.refinable = False  # a class inside a function: not import-time state
            return
        for name in names:
            self._info.keys.add((qual, name))

    # ---- per-insertion analysis on the new tree ---------------------------
    def _insertion(self, path, old_tree, old_lines, new_tree, new_line: int, point: int):
        chain = _chain(new_tree, new_line)
        funcs = [n for n in chain if isinstance(n, FUNC_TYPES)]
        if not funcs:
            self._touched_import_time = True
            self._unprobeable()
            stmts = [n for n in chain if isinstance(n, ast.stmt)]
            stmt = stmts[-1] if stmts else None
            self._record_keys(chain, stmt, new_line)
            classes = [n for n in chain if isinstance(n, ast.ClassDef)]
            if classes and _has_generic_fields(classes[-1], self.maps.enums):
                return self._generic_broad(path, classes, self._broad_new(path, old_tree, chain))
            # a new import-time statement runs where its neighbours ran: use their import groups
            return self._narrow(path, stmt, (path, max(point - 1, 1), point), self._broad_new(path, old_tree, chain))
        named = [n for n in chain if isinstance(n, NAMED_FUNCS)]
        if named:
            qual = _qualname(chain[: chain.index(named[-1]) + 1])
            old_func = _scopes(old_tree).get(qual)
            if old_func is None:
                self._info.refinable = False  # a new function or method
                self._unprobeable()
                return self._broad_new(path, old_tree, chain)
            if funcs[-1] is named[-1] and new_line < named[-1].body[0].lineno:
                self._info.refinable = False
                self._unprobeable()
                return self._range(path, _start(old_func), old_func.end_lineno)
        neighbours = (_code_line(old_lines, point - 1, -1), _code_line(old_lines, point, 1))
        return _union([self._old_line(path, old_tree, n) for n in neighbours if n is not None])

    def _broad_new(self, path: str, old_tree, new_chain) -> set[int] | None:
        classes = [n for n in new_chain if isinstance(n, ast.ClassDef)]
        if classes:
            qual = _qualname(new_chain[: new_chain.index(classes[-1]) + 1])
            old_cls = _scopes(old_tree).get(qual)
            if old_cls is not None:
                return self._broad(path, [old_cls])
        return self._broad(path, [])

    # ---- file-level entry points ------------------------------------------
    def tests_for(self, fd: FileDiff) -> set[int] | None:
        path = fd.path
        if not (path.startswith("sqlglot/") and path.endswith(".py")):
            self._unprobeable()
        if path.startswith("tests/fixtures/"):
            return self._fixture(fd)
        if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
            return self._python(fd)
        if path.endswith(DOC_SUFFIXES) or path.startswith(("docs/", ".github/")):
            return set()
        return ALL

    def _fixture(self, fd: FileDiff) -> set[int] | None:
        if fd.path.endswith(".sql") and not fd.binary and fd.old_path and fd.new_path:
            changed = [t for h in fd.hunks for t in h.changed_text()]
            if all(not t or t.startswith("--") for t in changed):
                return set()
        tests = self.maps.fixtures.get(fd.old_path or "") or self.maps.fixtures.get(fd.new_path or "")
        if tests:
            return set(tests)
        folder = os.path.dirname(fd.path) + "/"
        near = {n for f, ts in self.maps.fixtures.items() if f.startswith(folder) for n in ts}
        return near or ALL

    def _python(self, fd: FileDiff) -> set[int] | None:
        path = fd.path
        extra: set[int] = set()
        for prefix, test_file in PACKAGE_WALKERS.items():
            if path.startswith(prefix):
                extra |= self.maps.tests_by_file.get(test_file, set())
        if fd.old_path is None or fd.new_path is None or fd.old_path != fd.new_path:
            self._unprobeable()
        if fd.old_path is None:
            return extra  # a new module nothing imports yet
        if fd.new_path is None or fd.old_path != fd.new_path:
            return ALL  # deleted or renamed module
        try:
            with open(os.path.join(self.src_root, path), encoding="utf-8") as handle:
                old_text = handle.read()
            old_tree = ast.parse(old_text)
        except (OSError, SyntaxError, ValueError):
            return ALL
        old_lines = old_text.split("\n")
        old_key = module_key(old_tree, path)
        self.patched_sources[path] = "\n".join(apply_hunks(old_lines, fd.hunks))
        try:
            full_tree = ast.parse("\n".join(apply_hunks(old_lines, fd.hunks)))
            full_key = module_key(full_tree, path)
        except (SyntaxError, ValueError):
            self._unprobeable()
            return self._broken(path)
        self._single = (full_tree, full_key) if len(fd.hunks) == 1 else None
        if full_key == old_key or all(self.equiv.same_hunk_text(h, path) for h in fd.hunks):
            return set()
        self._touched_import_time = False
        self._info = RefineInfo(runtime=set(extra))
        tests = _union([self._hunk(path, old_tree, old_key, old_lines, h) for h in fd.hunks])
        if self._touched_import_time and path.startswith("sqlglot/"):
            self.import_time[path] = "\n".join(apply_hunks(old_lines, fd.hunks))
            self.refine_info[path] = self._info
        return ALL if tests is ALL else tests | extra

    def _broken(self, path: str) -> set[int] | None:
        if path.startswith(("tests/test_", "tests/dialects/test_")):
            return set(self.maps.tests_by_file.get(path, set()))
        return ALL

    def _hunk(self, path, old_tree, old_key, old_lines, hunk: Hunk):
        new_lines = apply_hunks(old_lines, [hunk])
        if self._single is not None:
            new_tree, new_key = self._single
        else:
            try:
                new_tree = ast.parse("\n".join(new_lines))
            except (SyntaxError, ValueError):
                self._info.refinable = False
                self._unprobeable()
                return self._broken(path)
            new_key = None
        if self.equiv.same_hunk_text(hunk, path):
            return set()
        if (new_key if new_key is not None else module_key(new_tree, path)) == old_key:
            return set()
        parts = [self._old_line(path, old_tree, line) for line in hunk.removed_lines()]
        for point, new_line in _added(hunk):
            if _is_code(new_lines[new_line - 1]):
                parts.append(self._insertion(path, old_tree, old_lines, new_tree, new_line, point))
        return _union(parts)
