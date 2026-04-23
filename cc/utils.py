"""Small helpers shared across compiler phases."""

from __future__ import annotations

import re
from dataclasses import fields
from typing import TYPE_CHECKING

from cc.ast_nodes import Node
from cc.errors import CompileError
from cc.tokens import CHARACTER_ESCAPES

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_ASSIGN_RE = re.compile(r"%assign\s+(\w+)\s+(.*?)(?:\s*;.*)?$")
_HEX_RE = re.compile(r"\b([0-9A-Fa-f]+)h\b")


def parse_asm_constants(path: Path, /) -> dict[str, int]:
    """Parse ``%assign NAME EXPR`` lines from a NASM ``.asm`` file.

    Returns a dict mapping each constant name to its integer value.
    Simple decimal and hex (``Nh``) literals are resolved in one pass;
    expression-based constants (e.g. ``DIRECTORY_NAME_LENGTH + 1``) are
    resolved in subsequent passes once their dependencies are known.
    Constants whose expressions still contain unresolvable names after
    all passes are silently omitted.
    """
    raw: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ASSIGN_RE.search(line)
        if m:
            raw[m.group(1)] = m.group(2).strip()

    resolved: dict[str, int] = {}
    changed = True
    while changed:
        changed = False
        for name, expr in raw.items():
            if name in resolved:
                continue
            # Convert NASM hex literals (4DEh → 0x4DE) and substitute
            # already-resolved names so Python's eval can handle the rest.
            py_expr = _HEX_RE.sub(lambda m: "0x" + m.group(1), expr)
            for known, val in resolved.items():
                py_expr = re.sub(r"\b" + re.escape(known) + r"\b", str(val), py_expr)
            try:
                value = eval(py_expr, {"__builtins__": {}})  # noqa: S307
                if isinstance(value, int):
                    resolved[name] = value
                    changed = True
            except Exception:  # noqa: BLE001, S110
                pass
    return resolved


def ast_contains(node: Node, predicate: Callable[[Node], bool], /) -> bool:
    """Return True if any node in the tree satisfies *predicate*.

    Generic AST walker used by several codegen predicates
    (``_name_is_reassigned``, ``_node_references_var``,
    ``_statement_references``).
    """
    if predicate(node):
        return True
    for node_field in fields(node):
        value = getattr(node, node_field.name)
        if isinstance(value, Node):
            if ast_contains(value, predicate):
                return True
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Node) and ast_contains(item, predicate):
                    return True
    return False


def decode_string_escapes(text: str, /) -> str:
    r"""Decode every C escape sequence in *text* to its literal character.

    Handles ``\n``/``\t``/``\r``/``\b``/``\0``/``\\``/``\"`` from
    :data:`CHARACTER_ESCAPES` plus ``\xNN`` hex escapes.  Unknown
    single-letter escapes are passed through unchanged — the NASM
    output is the consumer, and treating them as literal escape
    sequences for the downstream assembler keeps callers from having
    to double-escape assembler-visible backslashes.
    """
    result: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "x" and i + 3 < len(text):
                result.append(chr(int(text[i + 2 : i + 4], 16)))
                i += 4
                continue
            if nxt in CHARACTER_ESCAPES:
                result.append(chr(CHARACTER_ESCAPES[nxt]))
                i += 2
                continue
        result.append(text[i])
        i += 1
    return "".join(result)


def decode_first_character(text: str, /, *, line: int | None = None) -> int:
    """Return the byte value of the first character in a C string literal.

    Returns:
        The integer byte value of the decoded character.

    Raises:
        CompileError: If the text contains an unrecognized escape sequence.

    """
    if text[0] == "\\" and len(text) >= 2:
        if text[1] == "x" and len(text) >= 3:
            return int(text[2:], 16)  # noqa: FURB166
        if text[1] not in CHARACTER_ESCAPES:
            message = f"unknown escape sequence: '\\{text[1]}'"
            raise CompileError(message, line=line)
        return CHARACTER_ESCAPES[text[1]]
    return ord(text[0])


def string_byte_length(text: str) -> int:
    r"""Return the byte length of a C string literal, excluding the trailing null.

    Handles escape sequences (``\n``, ``\0``, ``\t``, etc.).
    The string in the AST is the raw content between quotes, e.g.
    ``Hello\n\0`` which decodes to 7 bytes (H e l l o LF NUL)
    but the printable length is 6 (excluding the trailing NUL).
    """
    length = 0
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            i += 2  # escape sequence = 1 decoded byte
        else:
            i += 1
        length += 1
    # Subtract the trailing \0 if present
    if text.endswith("\\0"):
        length -= 1
    return length
