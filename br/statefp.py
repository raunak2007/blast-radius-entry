"""Fingerprint the state sqlglot builds at import time, to see what a patch changed.

`snapshot()` maps "module::Qual.attr" (class attributes, nested classes included) and
"module::name" (other module globals) to a hash of the value's structure. Values are
compared by content, never by identity, so re-importing an unchanged module gives the
same snapshot. Functions hash their bytecode, defaults and closure contents; classes
hash their qualified name; other objects their own attributes.

Hashes use the process's string-hash seed, so only compare snapshots taken in one
process (the import checker takes both in the same forked child).
"""

from __future__ import annotations

import enum
import functools
import re
import sys
import types

MAX_DEPTH = 40
PREFIX = "sqlglot"
_ATOMS = (type(None), bool, int, float, complex, str, bytes)
# class attributes that record where a class was defined, not what it does
_METADATA = ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__firstlineno__",
             "__static_attributes__")


def _code(code: types.CodeType, depth: int, seen: "_Memo") -> int:
    consts = tuple(_code(c, depth + 1, seen) if isinstance(c, types.CodeType) else _fp(c, depth + 1, seen)
                   for c in code.co_consts)
    return hash(("code", code.co_qualname, code.co_code, consts, code.co_names))


class _Memo:
    """Finished hashes by object id, plus the objects being hashed right now.

    A result that met a cycle or the depth limit depends on where the walk entered,
    so it is not cached: two walks of equal state must give equal hashes.
    """

    def __init__(self) -> None:
        self.done: dict[int, int] = {}
        self.active: set[int] = set()
        self.cut = 0  # cycles or depth cut-offs met so far


def _fp(value: object, depth: int, seen: _Memo) -> int:
    kind = type(value)
    if kind in _ATOMS:
        return hash((kind.__name__, value))
    if depth > 0 and kind is types.FunctionType and _named(value):
        # a module- or class-level function referenced from a table: its own key carries
        # its code, so a body edit stays one change rather than many. Decided before the
        # memo: the same function met at depth 0 (as its own key) must hash its contents.
        return hash(("fnref", value.__module__, value.__qualname__))
    key = id(value)
    if key in seen.done:
        return seen.done[key]
    if depth > MAX_DEPTH or key in seen.active:
        seen.cut += 1
        return hash(("cut", kind.__qualname__))
    cuts = seen.cut
    seen.active.add(key)
    try:
        result = _compound(value, kind, depth, seen)
    finally:
        seen.active.discard(key)
    if seen.cut == cuts:
        seen.done[key] = result
    return result


def _compound(value: object, kind: type, depth: int, seen: "_Memo") -> int:
    d = depth + 1
    if kind in (tuple, list):
        if value and all(isinstance(v, type) for v in value):
            # often tuple({...classes...}) for isinstance checks: its order is id-based
            return hash(("classes", frozenset(_fp(v, d, seen) for v in value)))
        return hash((kind.__name__, tuple(_fp(v, d, seen) for v in value)))
    if kind in (set, frozenset):
        return hash(("set", frozenset(_fp(v, d, seen) for v in value)))
    if isinstance(value, dict):
        return hash((kind.__qualname__, frozenset((_fp(k, d, seen), _fp(v, d, seen)) for k, v in value.items())))
    if isinstance(value, type):
        return hash(("class", value.__module__, value.__qualname__))
    if isinstance(value, enum.Enum):
        return hash(("enum", type(value).__qualname__, value.name))
    if isinstance(value, types.FunctionType):
        cells = tuple(_fp(c.cell_contents, d, seen) if _filled(c) else 0 for c in value.__closure__ or ())
        return hash(("fn", _code(value.__code__, d, seen), _fp(value.__defaults__, d, seen),
                     _fp(value.__kwdefaults__, d, seen), cells))
    if isinstance(value, (staticmethod, classmethod)):
        return hash((kind.__name__, _fp(value.__func__, d, seen)))
    if isinstance(value, property):
        return hash(("prop", _fp(value.fget, d, seen), _fp(value.fset, d, seen)))
    if isinstance(value, functools.partial):
        return hash(("partial", _fp(value.func, d, seen), _fp(value.args, d, seen), _fp(value.keywords, d, seen)))
    if isinstance(value, (types.BuiltinFunctionType, types.MethodType, types.ModuleType)):
        return hash((kind.__name__, getattr(value, "__qualname__", None) or getattr(value, "__name__", "")))
    if isinstance(value, re.Pattern):
        return hash(("re", value.pattern, value.flags))
    if isinstance(value, types.CodeType):
        return _code(value, d, seen)
    if isinstance(value, (types.MappingProxyType, type({}.values()), type({}.keys()))):
        return hash((kind.__name__, _fp(list(value.items()) if hasattr(value, "items") else list(value), d, seen)))
    attrs = _attrs(value)  # a fresh dict: hash it inline, its id must not enter the memo
    if not attrs and type(value).__repr__ is not object.__repr__:
        text = repr(value)
        if " at 0x" not in text:
            return hash(("repr", kind.__qualname__, text))
    return hash(("obj", kind.__module__, kind.__qualname__,
                 frozenset((k, _fp(v, d, seen)) for k, v in attrs.items())))


def _named(fn: types.FunctionType) -> bool:
    return "<locals>" not in fn.__qualname__ and "<lambda>" not in fn.__qualname__


def _filled(cell: types.CellType) -> bool:
    try:
        cell.cell_contents
    except ValueError:
        return False
    return True


def _attrs(value: object) -> dict:
    out = dict(getattr(value, "__dict__", None) or {})
    for klass in type(value).__mro__:
        for slot in getattr(klass, "__slots__", ()) or ():
            if isinstance(slot, str) and slot not in out and hasattr(value, slot):
                out[slot] = getattr(value, slot)
    return out


_CALLABLE = (types.FunctionType, types.BuiltinFunctionType, types.MethodType, staticmethod, classmethod,
             property, functools.partial)


def _kind(value: object) -> str:
    if isinstance(value, type):
        return "class"
    return "callable" if isinstance(value, _CALLABLE) or callable(value) else "data"


def _class_entries(prefix: str, klass: type, module: str, out: dict, kinds: dict, seen: "_Memo") -> None:
    for name, value in list(vars(klass).items()):
        if name in _METADATA:
            continue
        key = f"{prefix}.{name}"
        if isinstance(value, type) and value.__module__ == module and value.__qualname__ == f"{klass.__qualname__}.{name}":
            _class_entries(key, value, module, out, kinds, seen)
        out[key] = _fp(value, 0, seen)
        kinds[key] = _kind(value)


def snapshot(modules: list[str] | None = None) -> tuple[dict[str, int], dict[str, str]]:
    """(fingerprints, kinds) for the given module names (default: every loaded sqlglot
    module). Kinds are "class", "callable" or "data"."""
    names = modules if modules is not None else [n for n in sys.modules if n == PREFIX or n.startswith(PREFIX + ".")]
    out: dict[str, int] = {}
    kinds: dict[str, str] = {}
    seen = _Memo()
    for name in sorted(names):
        module = sys.modules.get(name)
        if module is None:
            continue
        for attr, value in list(vars(module).items()):
            if attr.startswith("__") or isinstance(value, types.ModuleType):
                continue
            key = f"{name}::{attr}"
            if isinstance(value, type) and value.__module__ == name and value.__qualname__ == attr:
                _class_entries(key, value, name, out, kinds, seen)
            out[key] = _fp(value, 0, seen)
            kinds[key] = _kind(value)
    return out, kinds


def diff(before: dict[str, int], after: dict[str, int]) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def kind_of(before: tuple[dict, dict], after: tuple[dict, dict], key: str) -> str:
    """The stricter kind of `key` across two snapshots ("data" only if data in both)."""
    kinds = {before[1].get(key, "data"), after[1].get(key, "data")}
    for kind in ("class", "callable"):
        if kind in kinds:
            return kind
    return "data"
