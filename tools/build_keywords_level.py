"""Make tokenizer KEYWORDS entries narrowable per key, adding to "key_level" in dictkeys.pkl.gz.

build_dictkeys.py rules KEYWORDS out because code that runs at import time reads it. Every
such read is per key, or depends only on keys with certain characters:
  - a dialect's class body pops a literal key (`KEYWORDS.pop("DIV")`), and the Dialect
    metaclass checks `"(+)" in ...KEYWORDS`: those keys,
  - the Tokenizer metaclass checks `cls.HINT_START in cls.KEYWORDS`: every HINT_START value,
  - the Tokenizer metaclass builds `_KEYWORD_TRIE` from the keys that contain a space or a
    single-token character: any key with one of those characters,
  - tokenizers made at import time (parses run while modules import) look words up: the
    words they looked up, traced here at each lookup in tokenizer_core.py.
Whole reads at runtime come from tokenizer classes made inside tests, which full_tests
already keeps. This checks that the import-time reads are exactly the ones listed, then
records the keys (and characters) an edit must not touch.

usage: python build_keywords_level.py <sqlglot checkout> <dictkeys.pkl.gz in> <out.pkl.gz>
"""
import ast
import gzip
import json
import pickle
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
in_path, out_path = sys.argv[2:4]
ATTR = "KEYWORDS"
data = pickle.load(gzip.open(in_path))

# where an import-time tokenizer looks a keyword up, and the key it uses there
LOOKUPS = {
    ("sqlglot/tokenizer_core.py", 805): "keyword",
    ("sqlglot/tokenizer_core.py", 893): "word",
    ("sqlglot/tokenizer_core.py", 1047): 'numeric_literals.get(numeric_literal.upper(), "")',
    ("sqlglot/tokenizer_core.py", 1188): "self.sql[self._start : self._current].upper()",
    ("sqlglot/tokenizer_core.py", 1191): "self.sql[self._start : self._current].upper()",
}

TRACE = r"""
import json, os, sys
root, lookups = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, root)
want = {(os.path.join(root, rel), line): compile(expr, "<key>", "eval") for (rel, line), expr in lookups}
files = {f for f, _ in want}
got, bad, lines_seen = set(), [], set()
def tracer(frame, event, arg):
    if frame.f_code.co_filename not in files:
        return None
    def local(frame, event, arg):
        if event == "line":
            code = want.get((frame.f_code.co_filename, frame.f_lineno))
            if code is not None:
                try:
                    key = eval(code, frame.f_globals, frame.f_locals)
                except Exception as e:
                    bad.append(repr(e))
                else:
                    got.add(key if isinstance(key, str) else repr(key))
        return local
    return local
sys.settrace(tracer)
import importlib, pkgutil
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
sys.settrace(None)
import inspect
hints, chars = set(), set()
seen = set()
for mod in list(sys.modules.values()):
    if not getattr(mod, "__name__", "").startswith("sqlglot"):
        continue
    for obj in list(vars(mod).values()):
        stack = [obj]
        while stack:
            cls = stack.pop()
            if not inspect.isclass(cls) or id(cls) in seen:
                continue
            seen.add(id(cls))
            stack.extend(v for v in vars(cls).values() if inspect.isclass(v))
            try:
                stack.extend(type.__subclasses__(cls))
            except TypeError:
                pass
            single = getattr(cls, "SINGLE_TOKENS", None)
            if isinstance(single, dict):
                for k in single:
                    if isinstance(k, str):
                        chars.update(k)
            hint = getattr(cls, "HINT_START", None)
            if isinstance(hint, str) and isinstance(getattr(cls, "KEYWORDS", None), dict):
                hints.add(hint)
print(json.dumps({"got": sorted(got), "bad": bad, "hints": sorted(hints), "chars": sorted(chars)}))
"""
lookups_json = json.dumps([[list(k), v] for k, v in LOOKUPS.items()])
traced = json.loads(subprocess.run([sys.executable, "-I", "-c", TRACE, str(root), lookups_json],
                                   capture_output=True, text=True, check=True).stdout)
if traced["bad"]:
    sys.exit(f"could not read a key: {traced['bad'][:3]}")

# the lines that run at import time, as build_dictkeys.py traced them
doc_lines = {}
TRACE_LINES = r"""
import json, os, sys
root = sys.argv[1]
sys.path.insert(0, root)
seen = set()
prefix = os.path.join(root, "sqlglot") + os.sep
def tracer(frame, event, arg):
    fn = frame.f_code.co_filename
    if not fn.startswith(prefix):
        return None
    def local(frame, event, arg):
        if event == "line":
            seen.add((fn, frame.f_lineno))
        return local
    seen.add((fn, frame.f_lineno))
    return local
sys.settrace(tracer)
import importlib, pkgutil
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
sys.settrace(None)
print(json.dumps(sorted([os.path.relpath(f, root).replace(os.sep, "/"), n] for f, n in seen)))
"""
for rel, n in json.loads(subprocess.run([sys.executable, "-I", "-c", TRACE_LINES, str(root)], capture_output=True,
                                        text=True, check=True).stdout):
    doc_lines.setdefault(rel, set()).add(n)

# every import-time read of KEYWORDS, found the way build_dictkeys.py finds them
reads = []


class Reads(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel, self.fd, self.cd, self.target = rel, 0, 0, None

    def report(self, node):
        if not (self.fd == 0 or node.lineno in doc_lines.get(self.rel, ())):
            return
        if self.fd == 0 and self.cd and self.target == ATTR:
            return
        reads.append((self.rel, node.lineno))

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load) and node.attr == ATTR:
            self.report(node)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and self.cd and self.fd == 0 and node.id == ATTR:
            self.report(node)

    def visit_FunctionDef(self, node):
        for d in node.decorator_list + node.args.defaults + [x for x in node.args.kw_defaults if x]:
            self.visit(d)
        self.fd += 1
        for s in node.body:
            self.visit(s)
        self.fd -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.fd += 1
        self.visit(node.body)
        self.fd -= 1

    def visit_ClassDef(self, node):
        for b in node.bases + node.decorator_list + [k.value for k in node.keywords]:
            self.visit(b)
        self.cd += 1
        for s in node.body:
            self.visit(s)
        self.cd -= 1

    def _assign(self, targets, value):
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        saved = self.target
        self.target = names[0] if len(names) == 1 and len(targets) == 1 and self.cd and self.fd == 0 else None
        if value is not None:
            self.visit(value)
        self.target = saved

    def visit_Assign(self, node):
        self._assign(node.targets, node.value)

    def visit_AnnAssign(self, node):
        self._assign([node.target], node.value)


trees = {}
for p in sorted((root / "sqlglot").rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    trees[rel] = ast.parse(p.read_text(encoding="utf-8"))
    Reads(rel).visit(trees[rel])

popped, unexplained = set(), []
KNOWN = {("sqlglot/dialects/dialect.py", 421): {"(+)"},  # "(+)" in klass.tokenizer_class.KEYWORDS
         ("sqlglot/tokens.py", 114): set(traced["hints"]),  # cls.HINT_START in cls.KEYWORDS
         ("sqlglot/tokens.py", 119): set(),  # the keyword trie: covered by the character rule
         ("sqlglot/tokens.py", 579): set()}  # keywords=self.KEYWORDS: covered by the traced lookups
for rel, line in reads:
    if (rel, line) in KNOWN:
        popped |= KNOWN[(rel, line)]
        continue
    stmt = next((n for n in ast.walk(trees[rel]) if isinstance(n, ast.Expr) and n.lineno == line), None)
    call = stmt.value if stmt is not None else None
    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "pop"
            and isinstance(call.func.value, ast.Name) and call.func.value.id == ATTR and len(call.args) >= 1
            and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
        popped.add(call.args[0].value)
        continue
    unexplained.append((rel, line))
missing = [k for k in KNOWN if k not in set(reads)]
if unexplained or missing:
    sys.exit(f"unexplained import-time reads {unexplained[:5]}, expected but not found {missing}")

reasons = {r for r in data["unsafe"].get(ATTR, ())}
other = {r for r in reasons if not r.startswith("read at import time") and r != "read whole by sqlglot at runtime"}
if other:
    sys.exit(f"KEYWORDS has other reasons: {other}")

keys = popped | set(traced["got"])
data.setdefault("key_level", {})[ATTR] = {"s:" + k for k in keys}
data.setdefault("key_level_chars", {})[ATTR] = set(traced["chars"]) | {" "}
with gzip.open(out_path, "wb") as fh:
    pickle.dump(data, fh)
print(f"KEYWORDS: {len(reads)} import-time reads explained; {len(popped)} popped or checked keys, "
      f"{len(traced['got'])} words looked up while importing, {len(traced['chars'])} trie characters; wrote {out_path}")
