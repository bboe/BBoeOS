"""C preprocessor: ``#define`` / ``#include`` handling.

Only object-like ``#define`` macros and double-quoted ``#include``
directives are supported — the minimal subset BBoeOS source code uses.
"""

from __future__ import annotations

import re
from pathlib import Path

from cc.errors import CompileError
from cc.lexer import tokenize

INCLUDE_PATTERN = re.compile(r'\s*"([^"]+)"\s*$')


def apply_defines(
    *,
    defines: dict[str, str],
    tokens: list[tuple[str, str, int]],
) -> list[tuple[str, str, int]]:
    """Substitute IDENT tokens matching a ``#define`` with the macro's tokens.

    Each macro value is retokenized on every occurrence so the caller
    can use any token sequence (numbers, char literals, parenthesized
    expressions).  The substituted tokens inherit the line number of
    the original IDENT so diagnostics point at the use site, not the
    define site.  Substitution iterates to fixed point so ``#define
    B (A + 1)`` followed by ``#define A _program_end`` expands ``B`` to
    ``(_program_end + 1)`` in one pass rather than leaving the inner
    ``A`` untouched.  No function-like macros; an iteration cap
    (``MAX_ROUNDS``) guards against accidental self-reference cycles.
    """
    if not defines:
        return tokens
    max_rounds = 16
    for _ in range(max_rounds):
        result: list[tuple[str, str, int]] = []
        changed = False
        for kind, text, line in tokens:
            if kind == "IDENT" and text in defines:
                value_tokens = tokenize(defines[text])
                for value_kind, value_text, _drop in value_tokens[:-1]:  # drop trailing EOF
                    result.append((value_kind, value_text, line))
                changed = True
            else:
                result.append((kind, text, line))
        if not changed:
            return result
        tokens = result
    message = f"#define expansion exceeded {max_rounds} rounds; cycle?"
    raise CompileError(message)


def preprocess(
    source: str,
    /,
    *,
    include_base: Path | None = None,
    include_stack: frozenset[Path] = frozenset(),
) -> tuple[str, dict[str, str]]:
    """Expand ``#include "..."`` directives and collect ``#define`` macros.

    Only object-like ``#define NAME VALUE`` macros are supported.  Each
    ``#define`` line is replaced with a blank line so downstream line
    numbers stay correct.

    ``#include`` accepts only the double-quoted form and resolves the
    path relative to *include_base* (the directory of the source
    currently being preprocessed) — matching NASM's ``%include``.
    Included files are preprocessed recursively; their ``#define``
    entries merge into the outer pool so later definitions override.
    ``include_stack`` carries the set of files currently being
    expanded so a cycle is rejected with a clear error.  The directive
    line itself is replaced by the included file's processed text, so
    error line numbers after an include shift by the included file's
    length — acceptable in the absence of ``#line`` support.

    Returns:
        (processed_source, defines).  ``defines`` maps each macro name
        to the raw value text, which is retokenized at substitution
        time so the tokens inherit the current position's line number.

    """
    if include_base is None:
        include_base = Path()
    defines: dict[str, str] = {}
    output_lines: list[str] = []
    for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#include"):
            match = INCLUDE_PATTERN.match(stripped[len("#include") :])
            if match is None:
                message = f"malformed #include: {line.rstrip()!r}"
                raise CompileError(message, line=line_number)
            include_path = (include_base / match.group(1)).resolve()
            if include_path in include_stack:
                message = f"circular #include of {include_path}"
                raise CompileError(message, line=line_number)
            try:
                included_source = include_path.read_text(encoding="utf-8")
            except OSError as error:
                message = f"cannot open #include file {match.group(1)!r}: {error}"
                raise CompileError(message, line=line_number) from error
            included_text, included_defines = preprocess(
                included_source,
                include_base=include_path.parent,
                include_stack=include_stack | {include_path},
            )
            defines.update(included_defines)
            output_lines.append(included_text)
            continue
        if not stripped.startswith("#define"):
            output_lines.append(line)
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            message = f"malformed #define: {line.rstrip()!r}"
            raise CompileError(message, line=line_number)
        name = parts[1]
        value = parts[2].rstrip()
        if not value:
            message = f"empty #define value for {name!r}"
            raise CompileError(message, line=line_number)
        defines[name] = value
        output_lines.append("\n")  # Preserve line numbering.
    return "".join(output_lines), defines
