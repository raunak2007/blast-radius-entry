"""Make some unsafe dict attributes narrowable per key, adding "key_level" to dictkeys.pkl.gz.

build_dictkeys.py rules out a whole attribute name when code that runs at import time reads
it, or when something writes it at runtime. For the parser's function tables those reads
and writes are per key: a parse at import time looks up the function names it meets, and
the writes add or remove one literal key. An edit to any other key can't reach them.

For each attribute in SPEC, every reason build_dictkeys.py gave must be one this explains:
  - each "read at import time in <file>:<line>" line is listed with the expression of the
    key it reads; this traces a fresh `import` of every sqlglot module and records that
    key each time such a line runs,
  - "written at runtime" only if every write to the name in sqlglot/ and tests/ is a
    subscript store, pop, setdefault or del with a literal string key.
The attribute then gets the set of keys an edit must not touch (canonical "s:<key>").

usage: python build_key_level.py <sqlglot checkout> <dictkeys.pkl.gz in> <out.pkl.gz>
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

# attribute -> {(file, line): key expression}, as build_dictkeys.py reported them
SPEC = {
    "FUNCTIONS": {("sqlglot/parser.py", 7667): "upper"},
    "FUNCTION_PARSERS": {("sqlglot/parser.py", 7646): "upper"},
    "NO_PAREN_FUNCTION_PARSERS": {("sqlglot/parser.py", 7245): "token.text.upper()",
                                  ("sqlglot/parser.py", 7621): "upper"},
}

data = pickle.load(gzip.open(in_path))

TRACE = r"""
import json, os, sys
root, spec = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, root)
want = {}
for attr, lines in spec.items():
    for (rel, line), expr in lines:
        want.setdefault((os.path.join(root, rel), line), []).append((attr, compile(expr, "<key>", "eval")))
files = {f for f, _ in want}
got = {attr: set() for attr in spec}
bad = []
def tracer(frame, event, arg):
    if frame.f_code.co_filename not in files:
        return None
    def local(frame, event, arg):
        if event == "line":
            hit = want.get((frame.f_code.co_filename, frame.f_lineno))
            if hit:
                for attr, code in hit:
                    try:
                        key = eval(code, frame.f_globals, frame.f_locals)
                    except Exception as e:
                        bad.append(f"{attr}: {e!r}")
                        continue
                    got[attr].add(key if isinstance(key, str) else repr(key))
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
print(json.dumps({"got": {a: sorted(v) for a, v in got.items()}, "bad": bad}))
"""
spec_json = json.dumps({a: [[list(k), v] for k, v in lines.items()] for a, lines in SPEC.items()})
proc = subprocess.run([sys.executable, "-I", "-c", TRACE, str(root), spec_json], capture_output=True, text=True,
                      check=True)
traced = json.loads(proc.stdout)
if traced["bad"]:
    sys.exit(f"could not read a key: {traced['bad'][:3]}")


def literal_writes(attr):
    """Literal string keys written to `attr` anywhere in sqlglot/ and tests/ (outside the dict
    literals that define it), or None if some write isn't to one literal key."""
    keys = set()
    for base in ("sqlglot", "tests"):
        for p in sorted((root / base).rglob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                target = None
                if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
                    for t in (node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]):
                        if isinstance(t, ast.Subscript) and getattr(t.value, "attr", getattr(t.value, "id", None)) == attr:
                            target = t.slice
                            if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
                                return None
                            keys.add(target.value)
                        elif isinstance(t, (ast.Attribute, ast.Name)) and getattr(t, "attr", getattr(t, "id", None)) == attr \
                                and isinstance(node, ast.AugAssign):
                            return None
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    owner = node.func.value
                    if getattr(owner, "attr", getattr(owner, "id", None)) != attr:
                        continue
                    if node.func.attr in ("pop", "setdefault"):
                        arg = node.args[0] if node.args else None
                        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                            return None
                        keys.add(arg.value)
                    elif node.func.attr in ("update", "clear", "popitem", "__setitem__", "__delitem__"):
                        return None
    return keys


key_level = {}
for attr, lines in SPEC.items():
    reasons = set(data["unsafe"].get(attr, ()))
    explained = {f"read at import time in {rel}:{line}" for rel, line in lines}
    writes = None
    if "written at runtime" in reasons:
        writes = literal_writes(attr)
        if writes is None:
            print(f"{attr}: a write isn't to one literal key; stays unsafe")
            continue
        explained.add("written at runtime")
    if len(reasons) >= 5 or not reasons <= explained:  # build_dictkeys.py keeps at most 5 reasons
        print(f"{attr}: unexplained reasons {sorted(reasons - explained)}; stays unsafe")
        continue
    keys = set(traced["got"][attr]) | (writes or set())
    key_level[attr] = {"s:" + k for k in keys}
    print(f"{attr}: {len(traced['got'][attr])} keys read at import time, {len(writes or ())} written: "
          f"{sorted(keys)[:12]}")

data["key_level"] = key_level
with gzip.open(out_path, "wb") as fh:
    pickle.dump(data, fh)
print(f"wrote {out_path}")
