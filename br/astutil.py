"""Small AST helpers shared by the impact analysis and the reader index."""

from __future__ import annotations

import ast

FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def start(node: ast.AST) -> int:
    """First line of a node, counting decorators."""
    decos = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decos])


def chain(tree: ast.AST, line: int) -> list[ast.AST]:
    """Nodes (outermost first) whose line range contains `line`."""
    out: list[ast.AST] = []
    node = tree
    while True:
        nxt = None
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno") or getattr(child, "end_lineno", None) is None:
                continue
            if start(child) <= line <= child.end_lineno:
                nxt = child
                if isinstance(child, (ast.stmt, ast.Lambda)):
                    break
        if nxt is None:
            return out
        out.append(nxt)
        node = nxt


def own_lines(stmt: ast.stmt) -> tuple[int, int]:
    """Lines a statement executes itself: a simple statement's whole span, or a compound
    statement's header (condition, iterator, context managers) up to its first child."""
    children = [
        child.lineno
        for field in ("body", "handlers", "orelse", "finalbody", "cases")
        for child in getattr(stmt, field, None) or []
        if hasattr(child, "lineno")
    ]
    if not children:
        return start(stmt), stmt.end_lineno
    return start(stmt), max(start(stmt), min(children) - 1)
