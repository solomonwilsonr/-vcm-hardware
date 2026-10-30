"""Minimal S-expression parser for KiCad files (iterative, no recursion limit issues)."""
from __future__ import annotations

Node = list  # list of str | Node; first element is the head symbol


class SexprError(ValueError):
    pass


def parse(text: str) -> Node:
    """Parse text containing exactly one top-level expression."""
    stack: list[Node] = []
    root: Node | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            node: Node = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
            i += 1
        elif c == ")":
            if not stack:
                raise SexprError(f"unbalanced ')' at offset {i}")
            done = stack.pop()
            if not stack:
                if root is not None:
                    raise SexprError("multiple top-level expressions")
                root = done
            i += 1
        elif c == '"':
            i += 1
            buf: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                    buf.append({"n": "\n", "t": "\t"}.get(text[i], text[i]))
                else:
                    buf.append(text[i])
                i += 1
            if i >= n:
                raise SexprError("unterminated string")
            i += 1
            if not stack:
                raise SexprError("string outside list")
            stack[-1].append("".join(buf))
        else:
            j = i
            while j < n and text[j] not in " \t\r\n()":
                j += 1
            if not stack:
                raise SexprError(f"atom outside list at offset {i}")
            stack[-1].append(text[i:j])
            i = j
    if stack:
        raise SexprError("unbalanced '(': missing ')'")
    if root is None:
        raise SexprError("empty input")
    return root


def head(node) -> str | None:
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else None


def children(node: Node, name: str) -> list[Node]:
    return [c for c in node[1:] if head(c) == name]


def child(node: Node, name: str) -> Node | None:
    for c in node[1:]:
        if head(c) == name:
            return c
    return None


def atoms(node: Node | None) -> list[str]:
    """String/atom arguments of a node (skipping sub-lists)."""
    return [a for a in (node or [])[1:] if isinstance(a, str)]


def dump(node) -> str:
    """Serialise back to text (used in tests for round-tripping)."""
    if isinstance(node, str):
        needs_quote = node == "" or any(ch in node for ch in ' ()"\\\t\n')
        return '"' + node.replace("\\", "\\\\").replace('"', '\\"') + '"' if needs_quote else node
    return "(" + " ".join(dump(x) for x in node) + ")"
