"""Build blast_map.pkl.gz from one instrumented run of sqlglot's tests at the pinned commit.

Everything in the map comes from the pinned commit itself (coverage, file opens,
dialect and expression usage, imports), which the track rules allow. Nothing in it
is derived from patches or from the sample answers.

usage: python build_map.py <sqlglot repo> <coverage db> <trace json prefix> \
           <usage json prefix> <tests.json> <out.pkl.gz>
"""
import ast
import collections
import glob
import gzip
import json
import os
import pickle
import sqlite3
import sys

from coverage.numbits import numbits_to_nums

repo, cov_db, trace_prefix, usage_prefix, tests_path, out_path = sys.argv[1:7]
repo = os.path.realpath(repo)
sys.path.insert(0, repo)

tests_doc = json.load(open(tests_path))
ids = [t["id"] for t in tests_doc["tests"]]
index = {t: i for i, t in enumerate(ids)}
ALL = (1 << len(ids)) - 1

by_file = collections.defaultdict(int)
by_class = collections.defaultdict(int)
for i, t in enumerate(ids):
    parts = t.split("::")
    by_file[parts[0]] |= 1 << i
    if len(parts) >= 3:
        by_class["::".join(parts[:2])] |= 1 << i


def node_mask(nodeid):
    i = index.get(nodeid)
    return (1 << i) if i is not None else 0


def class_mask_of(nodeid):
    parts = nodeid.split("::")
    if len(parts) >= 3:
        return by_class.get("::".join(parts[:2]), 0)
    return by_file.get(parts[0], 0)


def key_mask(key):
    if key.startswith("CLASS:"):
        return by_class.get(key[6:], 0)
    if key.startswith("FILE:"):
        return by_file.get(key[5:], 0)
    return node_mask(key)


# ---- line coverage -------------------------------------------------------
con = sqlite3.connect(cov_db)
files = {fid: os.path.relpath(os.path.realpath(p), repo) for fid, p in con.execute("select id, path from file")}
contexts = {cid: c for cid, c in con.execute("select id, context from context")}
line_mask = collections.defaultdict(lambda: collections.defaultdict(int))
import_lines = collections.defaultdict(set)
unmatched = collections.Counter()
for fid, cid, bits in con.execute("select file_id, context_id, numbits from line_bits"):
    rel = files[fid]
    if not rel.startswith("sqlglot/"):
        continue
    ctx = contexts[cid]
    lines = numbits_to_nums(bits)
    if not ctx:
        import_lines[rel].update(lines)
        continue
    nodeid, _, phase = ctx.partition("|")
    m = node_mask(nodeid) if phase == "run" else class_mask_of(nodeid)
    if not m:
        unmatched[ctx.split("|")[0].split("::")[0]] += 1
        continue
    per = line_mask[rel]
    for ln in lines:
        per[ln] |= m

masks, mask_id = [], {}
lines_out = {}
module_any = {}
for rel, per in line_mask.items():
    d = {}
    anym = 0
    for ln, m in per.items():
        if m not in mask_id:
            mask_id[m] = len(masks)
            masks.append(m)
        d[ln] = mask_id[m]
        anym |= m
    lines_out[rel] = d
    module_any[rel] = anym

# ---- file opens (fixtures and other data files) ----------------------------
file_opens = collections.defaultdict(int)
config_files = set()
subprocess_tests = 0
for path in glob.glob(trace_prefix + "*"):
    for key, rels in json.load(open(path)).items():
        if key == "<subprocess>":
            for test_key in rels:
                subprocess_tests |= key_mask(test_key)
            continue
        for rel in rels:
            if key == "UNKNOWN":
                config_files.add(rel)
            else:
                file_opens[rel] |= key_mask(key)

# ---- dialect and expression usage -------------------------------------------
dialect_users = collections.defaultdict(int)
expr_users = collections.defaultdict(int)
dialect_mro, expr_mro = {}, {}
for path in glob.glob(usage_prefix + "*"):
    doc = json.load(open(path))
    for key, names in doc["dialects"].items():
        m = key_mask(key)
        for n in names:
            dialect_users[n] |= m
    for key, names in doc["exprs"].items():
        m = key_mask(key)
        for n in names:
            expr_users[n] |= m
    dialect_mro.update(doc["dialect_mro"])
    expr_mro.update(doc["expr_mro"])

# expression class -> tests that built it or any subclass of it
expr_family_users = collections.defaultdict(int)
for cls, mro in expr_mro.items():
    m = expr_users.get(cls, 0)
    for anc in mro:
        expr_family_users[anc] |= m

# ---- which dialects each sqlglot class serves --------------------------------
import importlib
import pkgutil
import sqlglot
from sqlglot.dialects.dialect import Dialect, Dialects

for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name != "sqlglot.__main__":
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
for name in Dialects:
    try:
        Dialect.get_or_raise(name.value)
    except Exception:
        pass


def all_subclasses(cls):
    out, stack = set(), [cls]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub not in out:
                out.add(sub)
                stack.append(sub)
    return out


def source_key(cls):
    mod = sys.modules.get(cls.__module__)
    f = getattr(mod, "__file__", None)
    if not f:
        return None
    rel = os.path.relpath(os.path.realpath(f), repo)
    return (rel, cls.__qualname__) if rel.startswith("sqlglot/") else None


class_dialects = collections.defaultdict(set)
for d in all_subclasses(Dialect) | {Dialect}:
    related = list(d.__mro__)
    for attr in ("parser_class", "generator_class", "tokenizer_class", "jsonpath_tokenizer_class"):
        c = getattr(d, attr, None)
        if isinstance(c, type):
            related.extend(c.__mro__)
    for c in related:
        k = source_key(c)
        if k:
            class_dialects[k].add(d.__name__)

# dialect -> every dialect that inherits from it (its users count too)
dialect_desc = collections.defaultdict(set)
for name, mro in dialect_mro.items():
    for anc in mro:
        dialect_desc[anc].add(name)

class_users = {}
for k, names in class_dialects.items():
    m = 0
    for n in names:
        for sub in dialect_desc.get(n, {n}):
            m |= dialect_users.get(sub, 0)
    class_users[k] = m

# ---- names other sqlglot modules import from each module ----------------------
exported = collections.defaultdict(set)
for path in glob.glob(os.path.join(repo, "sqlglot", "**", "*.py"), recursive=True):
    try:
        tree = ast.parse(open(path, "rb").read())
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and node.module.startswith("sqlglot"):
            target = node.module.replace(".", "/")
            for cand in (target + ".py", target + "/__init__.py"):
                if os.path.exists(os.path.join(repo, cand)):
                    for alias in node.names:
                        exported[cand].add(alias.name)

doc = {
    "tests": ids,
    "masks": masks,
    "lines": lines_out,
    "module_any": module_any,
    "import_lines": {k: sorted(v) for k, v in import_lines.items()},
    "file_opens": dict(file_opens),
    "config_files": sorted(config_files | {"setup.cfg", "pyproject.toml", "setup.py", ".coveragerc", "tests/__init__.py", "tests/helpers.py", "tests/conftest.py", "conftest.py", "pytest.ini", "tox.ini"}),
    "class_users": class_users,
    "expr_family_users": dict(expr_family_users),
    "exported": {k: sorted(v) for k, v in exported.items()},
    "test_files": dict(by_file),
    "test_classes": dict(by_class),
    "subprocess_tests": subprocess_tests,
}
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump(doc, fh, protocol=pickle.HIGHEST_PROTOCOL)

print(f"tests {len(ids)}, distinct masks {len(masks)}, files with coverage {len(lines_out)}, "
      f"covered lines {sum(len(v) for v in lines_out.values())}")
print(f"fixture-like files {len(file_opens)}, config files {sorted(config_files)}")
print(f"class->dialect keys {len(class_users)}, expression classes {len(expr_family_users)}")
print(f"tests that start subprocesses: {[ids[i] for i in range(len(ids)) if subprocess_tests >> i & 1]}")
print(f"unmatched coverage contexts (test files not in tests.json): {dict(unmatched)}")
print(f"wrote {out_path}: {os.path.getsize(out_path) / 1e6:.2f} MB")
