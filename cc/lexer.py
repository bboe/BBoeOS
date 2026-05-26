"""C source tokenizer."""

from __future__ import annotations

from cc.errors import CompileError
from cc.tokens import KEYWORDS, TOKEN_PATTERN


def tokenize(source: str, /) -> list[tuple[str, str, int]]:
    """Tokenize C source code into a list of (kind, text, line) triples.

    Returns:
        A list of (kind, text, line) token triples.

    Raises:
        CompileError: If an unexpected character is encountered.

    """
    tokens: list[tuple[str, str, int]] = []
    position = 0
    line = 1
    while position < len(source):
        match = TOKEN_PATTERN.match(source, position)
        if not match:
            message = f"unexpected character {source[position]!r}"
            raise CompileError(message, line=line)
        kind = match.lastgroup
        assert kind is not None
        text = match.group()
        if kind in {"BLOCK_COMMENT", "LINE_COMMENT", "WS"}:
            line += text.count("\n")
        else:
            if kind == "IDENT" and text in KEYWORDS:
                kind = text.upper()
            elif kind == "NUMBER":
                # Strip any C literal suffix (``u`` / ``l`` / ``ul`` /
                # ``ull`` / ``f`` etc., any case).  cc.py has no real
                # long / unsigned / float distinction at the literal
                # level; the suffix just needs to lex so headers can
                # write ``-1L`` / ``0xFFFFFFFFu`` / ``1.0f`` without
                # breaking the parser.
                is_hex = len(text) > 2 and text[0] == "0" and text[1] in "xX"
                if is_hex:
                    stripped_index = len(text)
                    while stripped_index > 0 and text[stripped_index - 1] in "uUlL":
                        stripped_index -= 1
                    if stripped_index < len(text):
                        text = text[:stripped_index]
                else:
                    stripped_index = len(text)
                    while stripped_index > 0 and text[stripped_index - 1] in "fFuUlL":
                        stripped_index -= 1
                    if stripped_index < len(text):
                        text = text[:stripped_index]
                # Floating-point literals (``0.0``, ``1e3``) are
                # truncated to their integer part — cc.py accepts the
                # spelling so FP-returning stubs can ``return 0.0``
                # without a parse error.
                if not is_hex and ("." in text or "e" in text or "E" in text):
                    text = str(int(float(text)))
            tokens.append((kind, text, line))
        position = match.end()
    tokens.append(("EOF", "", line))
    return tokens
