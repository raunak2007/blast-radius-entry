"""Where module-level names are read, at the pinned commit (static analysis only).

For every name bound by a plain module-level assignment in sqlglot/, record each place
that could read its value:

  runtime   statement spans inside function or lambda bodies (the tests that executed
            those lines are the only ones that can observe a new value there)
  defaults  def default arguments: the value is used whenever that function is called
            without the argument, so the function body's span stands for the use
  unsafe    any read that runs at import time (module or class level, decorators,
            module-level comprehensions), or inside tests/ helpers and conftest files,
            where a new value could reach tests without any covered line showing it
  test_files  test modules that mention the name at all (all their tests are kept)

Matching is by name, conservatively: bare names count in the defining module, in any
module that imports the name (under any alias) and in any module with a star import;
attribute reads `x.NAME` count everywhere, and so does "NAME" given to getattr and the
like, or written anywhere in a module with a lazy module-level __getattr__.

usage: python build_globals.py <sqlglot checkout> <out.pkl.gz>
"""
import ast
import gzip
import pickle
import sys
from pathlib import Path

root = Path(sys.argv[1])
out_path = sys.argv[2]

src_files = sorted(p for p in (root / "sqlglot").rglob("*.py"))
test_files = sorted(p for p in (root / "tests").rglob("*.py"))


def rel(p):
    return p.relative_to(root).as_posix()


trees = {}
for p in src_files + test_files:
    try:
        trees[rel(p)] = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        trees[rel(p)] = None

# pass 1: names bound by module-level assignments in sqlglot/
defined = {}
for r, tree in trees.items():
    if tree is None or not r.startswith("sqlglot/"):
        continue
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for t in targets:
            if isinstance(t, ast.Name):
                defined.setdefault(t.id, set()).add(r)

# aliases: from X import NAME as A  (A then stands for NAME too), to a fixed point
alias_of = {n: {n} for n in defined}
changed = True
while changed:
    changed = False
    for r, tree in trees.items():
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.asname and a.name in alias_of:
                        for orig in list(alias_of[a.name]):
                            if orig not in alias_of.setdefault(a.asname, set()):
                                alias_of[a.asname].add(orig)
                                changed = True

watch = set(alias_of)


def stmt_header_span(stmt):
    """Lines a statement occupies before its body (all of it for simple statements)."""
    for field in ("body", "handlers", "orelse", "finalbody"):
        seq = getattr(stmt, field, None)
        if isinstance(seq, list) and seq and isinstance(seq[0], ast.AST) and hasattr(seq[0], "lineno"):
            return stmt.lineno, max(stmt.lineno, seq[0].lineno - 1)
    return stmt.lineno, stmt.end_lineno


def body_span(func):
    return func.body[0].lineno, func.end_lineno


refs = {}  # original name -> record


def record(name):
    return refs.setdefault(name, {"runtime": set(), "defaults": set(), "unsafe": False, "test_files": set()})


def note(ident, r, kind, span=None):
    for orig in alias_of.get(ident, ()):
        rec = record(orig)
        if kind == "unsafe":
            rec["unsafe"] = True
        elif kind == "test":
            rec["test_files"].add(r)
        else:
            rec[kind].add((r, span[0], span[1]))


class Walker(ast.NodeVisitor):
    def __init__(self, r, tree):
        self.r = r
        self.is_test = r.startswith("tests/")
        self.helper = self.is_test and (not Path(r).name.startswith("test_") or Path(r).name == "conftest.py")
        self.future_ann = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                              and any(a.name == "annotations" for a in n.names) for n in tree.body)
        # bare names count here only if this module defines, imports or star-imports them
        self.bare_ok = set()
        star = False
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == "*":
                        star = True
                    else:
                        self.bare_ok.add(a.asname or a.name)
        for n in tree.body:
            ts = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
            for t in ts:
                if isinstance(t, ast.Name):
                    self.bare_ok.add(t.id)
        self.star = star
        self.lazy_module = any(isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in tree.body)
        self.runtime = False  # inside a function or lambda body
        self.stmt = None  # innermost statement
        self.default_of = None  # (func node) while visiting a def's defaults at import time
        self.in_all = False

    def hit(self, ident):
        if ident not in watch:
            return
        if self.is_test:
            note(ident, self.r, "unsafe" if self.helper else "test")
            return
        if self.runtime:
            note(ident, self.r, "runtime", stmt_header_span(self.stmt))
        elif self.default_of is not None:
            note(ident, self.r, "defaults", body_span(self.default_of))
        else:
            note(ident, self.r, "unsafe")

    def generic_visit(self, node):
        if isinstance(node, ast.stmt):
            saved, self.stmt = self.stmt, node
            super().generic_visit(node)
            self.stmt = saved
        else:
            super().generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and (node.id in self.bare_ok or self.star or self.is_test):
            self.hit(node.id)

    def visit_AugAssign(self, node):
        saved, self.stmt = self.stmt, node
        if isinstance(node.target, ast.Name):
            self.hit(node.target.id)  # reads before it writes
        self.visit(node.target)
        self.visit(node.value)
        self.stmt = saved

    def visit_Attribute(self, node):
        self.hit(node.attr)
        self.visit(node.value)

    def visit_Constant(self, node):
        # a string can name a module attribute that is looked up dynamically: count strings
        # in modules with a lazy module __getattr__ (their re-export tables), and see
        # visit_Call for getattr-style lookups
        if isinstance(node.value, str) and not self.in_all and self.lazy_module:
            self.hit(node.value)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr", "setattr", "delattr") \
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            self.hit(node.args[1].value)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        saved, self.stmt = self.stmt, node
        for a in node.names:
            if self.runtime or self.is_test:
                self.hit(a.name)  # an import inside a function reads the value when it runs
        self.stmt = saved

    def visit_Assign(self, node):
        saved, self.stmt = self.stmt, node
        is_all = not self.runtime and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        saved_all, self.in_all = self.in_all, is_all
        for t in node.targets:
            self.visit(t)
        self.visit(node.value)
        self.in_all = saved_all
        self.stmt = saved

    def visit_AnnAssign(self, node):
        saved, self.stmt = self.stmt, node
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)
        if not self.future_ann and not self.runtime:
            self.visit(node.annotation)
        self.stmt = saved

    def _visit_args(self, args, func):
        """Defaults run when the def runs: at import time for module or class defs."""
        saved = self.default_of
        if not self.runtime:
            self.default_of = func
        for d in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
            self.visit(d)
        self.default_of = saved
        if not self.future_ann:
            for a in args.posonlyargs + args.args + args.kwonlyargs + [x for x in (args.vararg, args.kwarg) if x]:
                if a.annotation is not None:
                    self.visit(a.annotation)

    def visit_FunctionDef(self, node):
        saved, self.stmt = self.stmt, node
        for d in node.decorator_list:
            self.visit(d)
        self._visit_args(node.args, node)
        if node.returns is not None and not self.future_ann:
            self.visit(node.returns)
        was = self.runtime
        self.runtime = True
        for s in node.body:
            self.visit(s)
        self.runtime = was
        self.stmt = saved

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        saved = self.default_of
        self.default_of = None  # lambda defaults: treat like any import-time read unless at runtime
        for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
            self.visit(d)
        self.default_of = saved
        was = self.runtime
        self.runtime = True
        self.visit(node.body)
        self.runtime = was

    def visit_ClassDef(self, node):
        saved, self.stmt = self.stmt, node
        for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(d)
        for s in node.body:
            self.visit(s)
        self.stmt = saved


for r, tree in trees.items():
    if tree is None:
        continue
    Walker(r, tree).visit(tree)

out = {}
for name, rec in refs.items():
    if name not in defined:
        continue
    out[name] = {
        "runtime": sorted(rec["runtime"]),
        "defaults": sorted(rec["defaults"]),
        "unsafe": rec["unsafe"],
        "test_files": sorted(rec["test_files"]),
    }
for name in defined:
    out.setdefault(name, {"runtime": [], "defaults": [], "unsafe": False, "test_files": []})
with gzip.open(out_path, "wb") as fh:
    pickle.dump({"names": out, "defined": {k: sorted(v) for k, v in defined.items()}}, fh)
safe = sum(1 for v in out.values() if not v["unsafe"])
print(f"{len(out)} module-level names, {safe} with no import-time reads")
