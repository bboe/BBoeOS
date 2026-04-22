"""Small helpers shared across compiler phases."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from cc.ast_nodes import Node
from cc.errors import CompileError
from cc.tokens import CHARACTER_ESCAPES

if TYPE_CHECKING:
    from collections.abc import Callable


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
