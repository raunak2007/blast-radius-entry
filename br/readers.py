"""Find where a class- or module-level name is read, to narrow constant edits.

For an edit such as `WEEK_OFFSET = -2` inside `class TSQL`, only tests that execute a
statement reading `WEEK_OFFSET` can observe it. This index returns those statements,
or None when the name is read in a way we cannot bound: at import time (metaclasses,
class bodies, module tables), through a string (getattr), or under an import alias.
"""

from __future__ import annotations

import ast
import os
import re

from br.astutil import FUNC_TYPES, chain, own_lines

SOURCE_DIRS = ("sqlglot", "tests")
Site = tuple[str, int, int]


def _mentions(node: ast.AST | None, name: str) -> bool:
    if node is None:
        return False
    return any(
        (isinstance(n, ast.Name) and n.id == name) or (isinstance(n, ast.Attribute) and n.attr == name)
        for n in ast.walk(node)
    )


def _only_defines(stmt: ast.stmt, name: str) -> bool:
    """True when stmt binds `name` as a plain target and never reads it."""
    if isinstance(stmt, ast.Assign):
        targets, value = stmt.targets, stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        targets, value = [stmt.target], stmt.value
    else:
        return False
    binds = any(isinstance(t, ast.Name) and t.id == name for t in targets)
    return binds and not _mentions(value, name)


def _is_read_of(node: ast.AST, name: str) -> bool:
    """`name` or `<anything>.name` being loaded."""
    if isinstance(node, ast.Name):
        return node.id == name and isinstance(node.ctx, ast.Load)
    return isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.ctx, ast.Load)


def _is_self_store(node: ast.AST, kw: str) -> bool:
    """`self.kw = kw`."""
    return (
        isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute) and node.targets[0].attr == kw
        and isinstance(node.targets[0].value, ast.Name) and node.targets[0].value.id == "self"
        and isinstance(node.value, ast.Name) and node.value.id == kw
    )


def _assigns_slots(stmt: ast.stmt) -> bool:
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [getattr(stmt, "target", None)]
    return any(isinstance(t, ast.Name) and t.id == "__slots__" for t in targets)


def _local_alias(stmt: ast.stmt, node: ast.Attribute) -> str | None:
    """`local = self.kw` (the whole value is the fetch) -> "local"."""
    if isinstance(stmt, ast.Assign) and stmt.value is node and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


class ReaderIndex:
    def __init__(self, src_root: str, import_lines: dict[str, list[int]]) -> None:
        self.src_root = src_root
        self.import_lines = {f: set(lines) for f, lines in import_lines.items()}
        self._texts: dict[str, str] | None = None
        self._trees: dict[str, ast.AST] = {}
        self._consumers: dict[tuple, list[Site] | None] = {}
        self._runtime: dict[tuple, list[Site] | None] = {}

    def _all_texts(self) -> dict[str, str]:
        if self._texts is None:
            texts: dict[str, str] = {}
            for top in SOURCE_DIRS:
                for folder, _, files in os.walk(os.path.join(self.src_root, top)):
                    for name in files:
                        if name.endswith(".py"):
                            full = os.path.join(folder, name)
                            rel = os.path.relpath(full, self.src_root).replace(os.sep, "/")
                            with open(full, encoding="utf-8") as handle:
                                texts[rel] = handle.read()
            self._texts = texts
        return self._texts

    def tree(self, path: str) -> ast.AST:
        return self._tree(path)

    def _tree(self, path: str) -> ast.AST:
        if path not in self._trees:
            self._trees[path] = ast.parse(self._all_texts()[path])
        return self._trees[path]

    def runtime_reader_lines(self, name: str) -> list[Site] | None:
        """Statements that read `name` when code runs after import.

        For use once the import check has shown what import-time code derived: reads in
        class bodies and module code are skipped, and function code that also runs at
        import counts through its run-time coverage.
        """
        key = ("runtime", name)
        if key not in self._runtime:
            self._runtime[key] = self.reader_lines(name, ("", 0, -1), runtime=True)
        return self._runtime[key]

    def reader_lines(self, name: str, defining: Site, runtime: bool = False) -> list[Site] | None:
        """Statements (file, start, end) that read `name`, excluding its definition.

        None means "cannot bound": the caller must keep its broad selection.
        """
        word = re.compile(rf"\b{re.escape(name)}\b")
        quoted = re.compile(rf"""["']{re.escape(name)}["']""")
        def_path, def_start, def_end = defining
        out: list[Site] = []
        for path, text in self._all_texts().items():
            if name not in text or not word.search(text):
                continue
            if quoted.search(text) and self._quoted_read(path, name):
                return None
            for lineno, line in enumerate(text.split("\n"), start=1):
                if name not in line or not word.search(line) or line.lstrip().startswith("#"):
                    continue
                if path == def_path and def_start <= lineno <= def_end:
                    continue
                site = self._site(path, lineno, name, runtime)
                if site is None:
                    return None
                if site:
                    forwarded = self._forwarded(path, lineno, name, runtime)
                    out.extend(forwarded if forwarded is not None else [site])
        return out

    # ---- constants passed through a constructor keyword --------------------
    def _forwarded(self, path: str, lineno: int, name: str, runtime: bool) -> list[Site] | None:
        """Where the value goes when every read of `name` on this statement is a keyword
        argument `Cls(kw=<x>.name)` and `Cls.__init__` only stores it as `self.kw`.

        Fetching `.kw` is the only way the stored value can reach later code, so the
        statements that fetch it bound the tests that can observe the constant. None
        means "not a pure forward" and the caller keeps the forwarding statement itself.
        """
        stmts = [n for n in chain(self._tree(path), lineno) if isinstance(n, ast.stmt)]
        if not stmts:
            return None
        targets: set[tuple[str, str]] = set()
        reads = 0
        for node in ast.walk(stmts[-1]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                for kw in node.keywords:
                    if kw.arg and _is_read_of(kw.value, name):
                        targets.add((node.func.id, kw.arg))
            if _is_read_of(node, name):
                reads += 1
        if not targets or reads != len(targets):
            return None
        out: list[Site] = []
        for callee, kw in sorted(targets):
            sites = self._attr_consumers(callee, kw, runtime)
            if sites is None:
                return None
            out.extend(sites)
        return out

    def _stores_param(self, callee: str, kw: str) -> bool:
        """True when exactly one class `callee` exists and its __init__ does `self.kw = kw`
        and reads the parameter nowhere else."""
        found = []
        for path, text in self._all_texts().items():
            if f"class {callee}" in text and path.startswith("sqlglot/"):
                found += [n for n in ast.walk(self._tree(path)) if isinstance(n, ast.ClassDef) and n.name == callee]
        if len(found) != 1:
            return False
        inits = [n for n in found[0].body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
        if len(inits) != 1:
            return False
        stores = [s for s in ast.walk(inits[0]) if _is_self_store(s, kw)]
        param_reads = [n for n in ast.walk(inits[0]) if isinstance(n, ast.Name) and n.id == kw]
        return len(stores) == 1 and len(param_reads) == 1

    def _attr_consumers(self, callee: str, kw: str, runtime: bool) -> list[Site] | None:
        key = (callee, kw, runtime)
        if key not in self._consumers:
            self._consumers[key] = self._find_consumers(callee, kw, runtime)
        return self._consumers[key]

    def _find_consumers(self, callee: str, kw: str, runtime: bool) -> list[Site] | None:
        if not self._stores_param(callee, kw):
            return None
        word = re.compile(rf"\b{re.escape(kw)}\b")
        out: list[Site] = []
        for path, text in self._all_texts().items():
            if kw not in text or not path.startswith("sqlglot/"):
                continue
            tree = self._tree(path)
            for lineno, line in enumerate(text.split("\n"), start=1):
                if kw not in line or not word.search(line) or line.lstrip().startswith("#"):
                    continue
                sites = self._consumer_sites(path, tree, lineno, kw, runtime)
                if sites is None:
                    return None
                out.extend(sites)
        if any(kw in text for path, text in self._all_texts().items() if path.startswith("tests/")):
            return None  # tests may poke the stored attribute directly
        return out

    def _consumer_sites(self, path: str, tree: ast.AST, lineno: int, kw: str, runtime: bool) -> list[Site] | None:
        nodes = chain(tree, lineno)
        stmts = [n for n in nodes if isinstance(n, ast.stmt)]
        if not stmts:
            return None
        stmt = stmts[-1]
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return []  # a parameter named kw in a signature
        if _assigns_slots(stmt) or _is_self_store(stmt, kw):
            return []
        on_line = [n for n in ast.walk(stmt) if getattr(n, "lineno", None) == lineno or (
            isinstance(n, ast.keyword) and n.value.lineno == lineno)]
        out: list[Site] = []
        for node in on_line:
            if isinstance(node, ast.Constant) and node.value == kw:
                return None  # getattr(obj, "kw") and friends
            if isinstance(node, ast.Name) and node.id == kw and isinstance(node.ctx, ast.Load):
                site = self._site(path, lineno, kw, runtime)
                if site is None:
                    return None
                out += [site] if site else []
            if isinstance(node, ast.Attribute) and node.attr == kw:
                if not isinstance(node.ctx, ast.Load):
                    return None  # someone overwrites the stored value
                alias = _local_alias(stmt, node)
                func = next((n for n in reversed(nodes) if isinstance(n, FUNC_TYPES)), None)
                if alias and isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out += self._local_readers(path, func, alias, stmt)
                    continue
                site = self._site(path, lineno, kw, runtime)
                if site is None:
                    return None
                out += [site] if site else []
        return out

    def _local_readers(self, path: str, func: ast.AST, local: str, alias_stmt: ast.stmt) -> list[Site]:
        """Statements of `func` (nested functions included) that read the local `local`."""
        out: list[Site] = []
        for node in ast.walk(func):
            if node is alias_stmt or not isinstance(node, ast.stmt) or isinstance(node, FUNC_TYPES):
                continue
            start, end = own_lines(node)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == local and isinstance(sub.ctx, ast.Load) \
                        and start <= sub.lineno <= end:
                    out.append((path, start, end))
                    break
        return out

    def _quoted_read(self, path: str, name: str) -> bool:
        """True when `name` appears as a string that could name an attribute: anything but
        a key of a dict literal (SQL function tables such as FUNCTIONS = {"TIME_FORMAT": ...})."""
        try:
            tree = self._tree(path)
        except SyntaxError:
            return True
        keys = {id(k) for node in ast.walk(tree) if isinstance(node, ast.Dict) for k in node.keys if k is not None}
        return any(
            isinstance(node, ast.Constant) and node.value == name and id(node) not in keys
            for node in ast.walk(tree)
        )

    def _site(self, path: str, lineno: int, name: str, runtime: bool = False) -> Site | tuple | None:
        """Runtime statement at lineno; () to ignore (plain imports); None if unbounded."""
        try:
            nodes = chain(self._tree(path), lineno)
        except SyntaxError:
            return None
        stmts = [n for n in nodes if isinstance(n, ast.stmt)]
        if stmts and isinstance(stmts[-1], ast.ImportFrom):
            aliased = any(a.asname for a in stmts[-1].names if a.name == name)
            return None if aliased else ()
        if stmts and isinstance(stmts[-1], ast.Import):
            return ()
        in_function = any(isinstance(n, FUNC_TYPES) for n in nodes)
        if not in_function and stmts and _only_defines(stmts[-1], name):
            return ()  # another class or module binding the same name: a write, not a read
        if runtime and not in_function:
            return ()  # import-time read: its effect is in the import check's state diff
        if not in_function or (not runtime and lineno in self.import_lines.get(path, ())):
            return None
        start, end = own_lines(stmts[-1]) if stmts else (lineno, lineno)
        return path, min(start, lineno), max(end, lineno)
