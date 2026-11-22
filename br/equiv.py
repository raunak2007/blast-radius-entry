"""Cheap checks that an edit cannot change behaviour.

Two tests, both far cheaper than dumping syntax trees:

* compiled-bytecode equality: comments, blank lines and layout change only line tables,
  so two sources whose code objects (bytecode, constants, names, nested code) match
  behave the same.
* textual canonicalisation of a hunk's changed lines: `x == None` and `x is None` (and
  `!=` / `is not`, and identity-compared sqlglot Enums such as `ScopeType.ROOT`) are
  the same in sqlglot, whose only None-unsafe __eq__ lives in the executor sort wrapper.
"""

from __future__ import annotations

import re
import types

from br.diff import Hunk

EQ_UNSAFE_FILES = ("sqlglot/executor/env.py",)
_NONE_EQ = re.compile(r"==\s*None\b")
_NONE_NE = re.compile(r"!=\s*None\b")
_NONE_EQ_LEFT = re.compile(r"\bNone\s*==")
_NONE_NE_LEFT = re.compile(r"\bNone\s*!=")


def _code_key(code: types.CodeType) -> tuple:
    consts = tuple(
        _code_key(c) if isinstance(c, types.CodeType) else (type(c).__name__, repr(c)) for c in code.co_consts
    )
    return (
        code.co_name, code.co_code, consts, code.co_names, code.co_varnames, code.co_freevars,
        code.co_cellvars, code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
        code.co_flags, code.co_exceptiontable,
    )


def module_key(source, path: str) -> tuple:
    """Behavioural fingerprint of a module, from source text or an already-parsed tree."""
    return _code_key(compile(source, path, "exec", dont_inherit=True))


class Equivalence:
    def __init__(self, enums: frozenset[str]) -> None:
        names = "|".join(map(re.escape, sorted(enums)))
        self.enum_eq = re.compile(r"==(\s*)(" + names + r")\.") if enums else None
        self.enum_ne = re.compile(r"!=(\s*)(" + names + r")\.") if enums else None

    def _canon(self, line: str) -> str:
        line = _NONE_EQ.sub("is None", line)
        line = _NONE_NE.sub("is not None", line)
        line = _NONE_EQ_LEFT.sub("None is", line)
        line = _NONE_NE_LEFT.sub("None is not", line)
        if self.enum_eq is not None:
            line = self.enum_eq.sub(r"is\1\2.", line)
            line = self.enum_ne.sub(r"is not\1\2.", line)
        return " ".join(line.split())

    def same_hunk_text(self, hunk: Hunk, path: str) -> bool:
        """True when the hunk only swaps equivalent comparison forms."""
        if path in EQ_UNSAFE_FILES:
            return False
        removed = [l[1:] for l in hunk.lines if l.startswith("-")]
        added = [l[1:] for l in hunk.lines if l.startswith("+")]
        if not removed or len(removed) != len(added):
            return False
        return all(self._canon(a) == self._canon(b) for a, b in zip(removed, added))
