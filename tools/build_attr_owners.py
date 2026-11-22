"""Merge attrs_plugin outputs into attr_owners.pkl.gz, with test masks in blast_map's order.

usage: python build_attr_owners.py <attrs pickle prefix> <blast_map.pkl.gz> <out.pkl.gz> [<sqlglot checkout>]

Per class constant (rel, class qualname, name) that was wrapped: the mask of tests whose
reads resolved to it (setup and teardown count for the test's class), whether it was read
outside any test, and the same mask split by the line that did the reading ("sites":
{(file, line): mask}). Packed per source file. Also "raised": per sqlglot file, the lines an
exception surfaced on during the run. Built only from the pinned commit's run.

With the checkout given, "evidence" too: for a read site where the constant only gates the
rest of a condition, the lines a test must have run for the constant's value to have mattered
there. {(file, line, name): (value, lo, hi)} means: at this read the constant (negated an odd
number of times if value is False) is an operand of an `and` whose later operands do nothing
but compute a value and never raised in the recorded run; while the constant is `value` the
`and` goes on to them, and if it were the other boolean the `and` would stop at it with False.
The two runs then differ only where those operands came out true: the body of the `if` or
`while` the `and` is the whole test of was entered (lo..hi is the body's first statement), or,
anywhere else, the operand after a boolean-valued one was reached (lo..hi is that operand).
"""
import ast
import os
import glob
import gzip
import pickle
import sys

prefix, map_path, out_path = sys.argv[1:4]
source_root = sys.argv[4] if len(sys.argv) > 4 else None
with gzip.open(map_path, "rb") as fh:
    doc = pickle.load(fh)
bit = {t: 1 << i for i, t in enumerate(doc["tests"])}


def ctx_mask(ctx):
    if ctx.startswith("CLASS:"):
        return doc["test_classes"].get(ctx[6:], 0)
    if ctx.startswith("FILE:"):
        return doc["test_files"].get(ctx[5:], 0)
    return bit.get(ctx, 0)


wrapped, reads, outside, unmatched = set(), {}, set(), set()
sites, raised = {}, {}
parts = sorted(glob.glob(prefix + "*"))
for path in parts:
    with open(path, "rb") as fh:
        part = pickle.load(fh)
    wrapped.update(map(tuple, part["wrapped"]))
    for key, ctxs in part["reads"].items():
        for ctx in ctxs:
            if ctx == "IMPORT":
                outside.add(key)
                continue
            m = ctx_mask(ctx)
            if not m:
                unmatched.add(ctx)
            reads[key] = reads.get(key, 0) | m
    for (key, file, line), ctxs in part.get("sites", {}).items():
        per = sites.setdefault(tuple(key), {})
        for ctx in ctxs:
            if ctx != "IMPORT":
                per[(file, line)] = per.get((file, line), 0) | ctx_mask(ctx)
    for file, lines in part.get("raised", {}).items():
        raised.setdefault(file, set()).update(lines)
by_file = {}
for rel, qual, name in wrapped:
    entry = {"mask": reads.get((rel, qual, name), 0), "outside": (rel, qual, name) in outside}
    if sites:
        per = {site: m for site, m in sites.get((rel, qual, name), {}).items() if m}
        union = 0
        for m in per.values():
            union |= m
        assert union == entry["mask"], (rel, qual, name)
        entry["sites"] = per
    by_file.setdefault(rel, {})[(qual, name)] = entry
# attribute reads that are known to be plain data in the pinned sources: the parser's token
# slots, a token's fields, an expression's args and the properties that only look into them
PLAIN_ATTRS = frozenset(("_curr", "_next", "_prev", "token_type", "text", "args", "dialect", "this", "expression",
                         "expressions", "parent", "name", "key", "kind", "unit", "alias"))


def plain_operand(node):
    """Evaluating it can do nothing but compute a value."""
    allowed = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Name, ast.Constant, ast.Compare, ast.BoolOp, ast.boolop, ast.cmpop, ast.expr_context,
                            ast.Tuple, ast.Set, ast.List, ast.Not)):
            continue
        if isinstance(sub, ast.UnaryOp) and isinstance(sub.op, ast.Not):
            continue
        if isinstance(sub, ast.Attribute) and (sub.attr.isupper() or sub.attr in PLAIN_ATTRS or id(sub) in allowed):
            continue
        if isinstance(sub, ast.Call) and not sub.keywords:
            f = sub.func
            if isinstance(f, ast.Name) and f.id in ("isinstance", "len"):
                continue
            if isinstance(f, ast.Attribute) and f.attr == "get" and isinstance(f.value, ast.Attribute) \
                    and f.value.attr == "args" and all(isinstance(a, ast.Constant) for a in sub.args):
                allowed.add(id(f))
                continue
        return False
    return True


def bool_valued(node):
    if isinstance(node, ast.Compare):
        return all(isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)) for op in node.ops)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"


_parsed = {}


def parsed(file):
    if file not in _parsed:
        tree = ast.parse(open(os.path.join(source_root, file), encoding="utf-8").read())
        parent = {}
        by_line = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                by_line.setdefault((node.lineno, node.attr), []).append(node)
        _parsed[file] = (parent, by_line)
    return _parsed[file]


def site_evidence(file, line, name):
    parent, by_line = parsed(file)
    reads = by_line.get((line, name), [])
    if len(reads) != 1:
        return None
    cur, value = reads[0], True
    up = parent.get(cur)
    while isinstance(up, ast.UnaryOp) and isinstance(up.op, ast.Not):
        value = not value
        cur, up = up, parent.get(up)
    if not isinstance(up, ast.BoolOp) or not isinstance(up.op, ast.And):
        return None
    at = next(i for i, operand in enumerate(up.values) if operand is cur)
    rest = up.values[at + 1:]
    if not rest or not all(plain_operand(operand) for operand in rest):
        return None
    hit = raised.get(file, ())
    if any(n in hit for n in range(min(cur.lineno, rest[0].lineno), rest[-1].end_lineno + 1)):
        return None  # something raised on these lines in the recorded run
    holder = parent.get(up)
    if isinstance(holder, (ast.If, ast.While)) and holder.test is up and holder.body[0].lineno > up.end_lineno:
        return value, holder.body[0].lineno, holder.body[0].end_lineno
    if len(rest) >= 2 and bool_valued(rest[0]) and rest[1].lineno > max(rest[0].end_lineno, cur.end_lineno):
        return value, rest[1].lineno, rest[1].end_lineno
    return None


evidence = {}
if source_root and sites:
    for (rel, qual, name), per in sites.items():
        for (file, line) in per:
            key = (file, line, name)
            if key not in evidence and file.startswith("sqlglot/") and os.path.exists(os.path.join(source_root, file)):
                evidence[key] = site_evidence(file, line, name)
    evidence = {k: v for k, v in evidence.items() if v is not None}

print(f"{len(parts)} parts; {len(wrapped)} constants, {len(reads)} read in tests, {len(outside)} read outside tests; "
      f"{len(unmatched)} unmatched contexts; {sum(len(v) for v in sites.values())} read sites, "
      f"{len(evidence)} with evidence lines", file=sys.stderr)
with gzip.open(out_path, "wb", compresslevel=9) as fh:
    pickle.dump({"format": 1, "tests": doc["tests"],
                 "by_file": {rel: pickle.dumps(v, protocol=4) for rel, v in by_file.items()},
                 "raised": {rel: sorted(lines) for rel, lines in sorted(raised.items())},
                 "evidence": evidence}, fh, protocol=4)
