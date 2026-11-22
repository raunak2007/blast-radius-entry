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

import io
import tokenize
import types

from br.diff import Hunk

_SKIP_TOKENS = (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT,
                tokenize.ENDMARKER)
EQ_UNSAFE_FILES = ("sqlglot/executor/env.py",)


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
        self.enums = frozenset(enums)

    def _canon(self, line: str):
        """Token strings of one line with the equivalent comparison forms unified.

        Whitespace between tokens is dropped, but a string literal keeps its text exactly
        (collapsing whitespace inside one changes behaviour). A line that does not tokenize
        on its own (an open multi-line string, say) is compared verbatim."""
        tokens: list[tuple[int, str]] = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline):
                if tok.type in _SKIP_TOKENS:
                    continue
                tokens.append((tok.type, tok.string))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            if not tokens or any(q in line for q in "'\""):
                return "\0" + line.strip()
            # no quote on the line: only an unclosed bracket stopped it, the tokens are exact
        out = [s for _, s in tokens]
        for i, word in enumerate(out):
            nxt = out[i + 1] if i + 1 < len(out) else ""
            prev = out[i - 1] if i else ""
            if word in ("==", "!=") and (nxt == "None" or prev == "None" or (
                    self.enums and nxt in self.enums and i + 2 < len(out) and out[i + 2] == ".")):
                out[i] = "is" if word == "==" else "is not"
        return " ".join(out)

    def same_hunk_text(self, hunk: Hunk, path: str) -> bool:
        """True when the hunk only swaps equivalent comparison forms."""
        if path in EQ_UNSAFE_FILES:
            return False
        removed = [l[1:] for l in hunk.lines if l.startswith("-")]
        added = [l[1:] for l in hunk.lines if l.startswith("+")]
        if not removed or len(removed) != len(added):
            return False
        return all(self._canon(a) == self._canon(b) for a, b in zip(removed, added))
