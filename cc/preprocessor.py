"""C preprocessor: ``#define`` / ``#include`` / ``#ifndef`` handling.

Two ``#define`` shapes are supported:

* **Object-like:** ``#define NAME VALUE`` — the IDENT ``NAME`` expands
  to the token sequence produced by tokenizing ``VALUE``.  The
  bare-name form ``#define NAME`` (no value) is also accepted; it
  defines ``NAME`` as an empty object-like macro that expands to no
  tokens.  This shape exists primarily so the standard header-guard
  pattern (``#ifndef FOO_H`` / ``#define FOO_H`` / ... / ``#endif``)
  works without requiring a dummy value.
* **Function-like:** ``#define NAME(p1, p2, ...) BODY`` — at each call
  site ``NAME(arg_tokens, ...)``, the BODY tokens are emitted with
  every IDENT matching a parameter name replaced by that argument's
  token sequence.  Arguments are split on commas at paren-depth 0, so
  nested calls like ``MAX(foo(x, y), z)`` work.  An IDENT not followed
  by ``(`` is left alone (so ``int WEXITSTATUS;`` would still parse —
  though shadowing a macro is a bad idea).  Expansion iterates to a
  fixed point shared with object-like macros, so nested function-like
  macros (``A(x)`` -> ``B(x)`` -> ``(x + 1)``) collapse in one
  ``apply_defines`` call.

Conditional compilation is supported in a very limited form:

* ``#ifndef NAME`` / ``#endif`` — if ``NAME`` is already defined as
  either an object-like or function-like macro, every line up to the
  matching ``#endif`` is dropped (including any ``#define`` /
  ``#include`` inside the block).  Otherwise the block is processed
  normally.  ``#ifndef`` blocks may nest; each ``#endif`` closes the
  most recent open ``#ifndef``.  Unbalanced directives (a stray
  ``#endif`` or an ``#ifndef`` with no matching ``#endif`` before EOF)
  raise ``CompileError``.

  This is enough to make the standard header-guard pattern work, so
  the same header can be ``#include``d from multiple translation-unit
  fragments without duplicate-definition fallout.

Out of scope for this version:

* Stringification (``#x``)
* Token pasting (``a ## b``)
* Variadic macros (``...`` / ``__VA_ARGS__``)
* ``#undef``
* ``#ifdef`` (inverse of ``#ifndef``; trivial to add when needed)
* ``#if <constant-expression>`` (would require expression evaluation
  in the preprocessor — a much bigger lift)
* ``#else`` / ``#elif``

Only the double-quoted ``#include "..."`` form is recognised — the
angle-bracket form (``<stdio.h>``) is not.
"""

from __future__ import annotations

import re
from pathlib import Path

from cc.errors import CompileError
from cc.lexer import tokenize

INCLUDE_PATTERN = re.compile(r'\s*"([^"]+)"\s*$')

#: Matches ``#define NAME(p1, p2, ...) body`` — the open paren must
#: come **immediately** after the macro name with no intervening
#: whitespace (per C; ``#define FOO (x)`` is an object-like macro
#: whose value is the token sequence ``(x)``).  The body extends to
#: end of line.  Parameter names are validated by the caller.
FUNCTION_DEFINE_PATTERN = re.compile(
    r"\s*(?P<name>[A-Za-z_][A-Za-z_0-9]*)\((?P<params>[^)]*)\)\s*(?P<body>.*?)\s*$",
)

#: Token-kind sentinel for the EOF token appended by :func:`cc.lexer.tokenize`.
_EOF_KIND = "EOF"


def _collect_function_macro_arguments(
    *,
    name: str,
    start_index: int,
    tokens: list[tuple[str, str, int]],
) -> tuple[list[list[tuple[str, str, int]]], int]:
    """Parse ``( arg1, arg2, ... )`` starting at the LPAREN after a macro name.

    *start_index* points at the LPAREN token.  Returns the list of
    arguments (each a token list) and the index *after* the matching
    RPAREN.  Commas at paren-depth 0 separate arguments; commas inside
    nested parens belong to the current argument.  An unterminated
    invocation raises ``CompileError``.
    """
    assert tokens[start_index][0] == "LPAREN"
    arguments: list[list[tuple[str, str, int]]] = []
    current: list[tuple[str, str, int]] = []
    depth = 1
    index = start_index + 1
    invocation_line = tokens[start_index][2]
    while index < len(tokens):
        kind, _text, _line = tokens[index]
        if kind == _EOF_KIND:
            break
        if kind == "LPAREN":
            depth += 1
            current.append(tokens[index])
        elif kind == "RPAREN":
            depth -= 1
            if depth == 0:
                # MACRO() with no args yields zero arguments; otherwise
                # the final argument is whatever we accumulated.
                if arguments or current:
                    arguments.append(current)
                return arguments, index + 1
            current.append(tokens[index])
        elif kind == "COMMA" and depth == 1:
            arguments.append(current)
            current = []
        else:
            current.append(tokens[index])
        index += 1
    message = f"unterminated argument list invoking macro {name!r}"
    raise CompileError(message, line=invocation_line)


def _expand_function_macro(
    *,
    body_tokens: list[tuple[str, str, int]],
    call_line: int,
    invocation_args: list[list[tuple[str, str, int]]],
    name: str,
    params: tuple[str, ...],
) -> list[tuple[str, str, int]]:
    """Substitute argument token sequences into a function-macro body.

    Every IDENT in *body_tokens* whose text matches a parameter name
    is replaced by the corresponding entry in *invocation_args*; other
    tokens are emitted verbatim.  All emitted tokens carry *call_line*
    so diagnostics point at the use site rather than the define site.
    """
    if len(invocation_args) != len(params):
        message = f"macro {name!r} expects {len(params)} argument(s), got {len(invocation_args)}"
        raise CompileError(message, line=call_line)
    parameter_index = {parameter: index for index, parameter in enumerate(params)}
    expanded: list[tuple[str, str, int]] = []
    for kind, text, _line in body_tokens:
        if kind == "IDENT" and text in parameter_index:
            for arg_kind, arg_text, _arg_line in invocation_args[parameter_index[text]]:
                expanded.append((arg_kind, arg_text, call_line))
        else:
            expanded.append((kind, text, call_line))
    return expanded


def _parse_function_define(
    *,
    body_text: str,
    line_number: int,
    name: str,
    params_text: str,
) -> tuple[tuple[str, ...], list[tuple[str, str, int]]]:
    """Parse a function-like ``#define`` into ``(params, body_tokens)``.

    *params_text* is the comma-separated parameter list (already
    stripped of the surrounding parens).  Empty parens (``MACRO()``)
    yield a zero-parameter macro.  Each parameter must be a bare
    identifier; duplicates raise ``CompileError``.  *body_text* is
    tokenized once at define time; the trailing ``EOF`` token is
    dropped so substitution can concatenate without rescanning.
    """
    raw_params = [piece.strip() for piece in params_text.split(",")] if params_text.strip() else []
    seen: set[str] = set()
    for parameter in raw_params:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", parameter):
            message = f"malformed #define parameter for {name!r}: {parameter!r}"
            raise CompileError(message, line=line_number)
        if parameter in seen:
            message = f"duplicate #define parameter for {name!r}: {parameter!r}"
            raise CompileError(message, line=line_number)
        seen.add(parameter)
    body_tokens = [tok for tok in tokenize(body_text) if tok[0] != _EOF_KIND]
    return tuple(raw_params), body_tokens


def apply_defines(
    *,
    defines: dict[str, str],
    function_defines: dict[str, tuple[tuple[str, ...], list[tuple[str, str, int]]]] | None = None,
    tokens: list[tuple[str, str, int]],
) -> list[tuple[str, str, int]]:
    """Substitute IDENT tokens matching a ``#define`` with the macro's tokens.

    Object-like macros expand by retokenizing the stored value text on
    every occurrence.  Function-like macros consume the trailing
    ``( ... )`` argument list, substitute parameters into the
    pre-tokenized body, and append the result.  Both kinds share the
    same fixed-point loop, so a function-like body that itself names
    another (object- or function-like) macro is fully expanded.

    Substituted tokens inherit the line number of the original IDENT
    so diagnostics point at the use site, not the define site.  An
    iteration cap (``max_rounds``) guards against accidental
    self-reference cycles.
    """
    function_defines = function_defines or {}
    if not defines and not function_defines:
        return tokens
    max_rounds = 16
    for _ in range(max_rounds):
        result: list[tuple[str, str, int]] = []
        changed = False
        index = 0
        while index < len(tokens):
            kind, text, line = tokens[index]
            if kind == "IDENT" and text in function_defines:
                next_index = index + 1
                if next_index < len(tokens) and tokens[next_index][0] == "LPAREN":
                    arguments, after = _collect_function_macro_arguments(
                        name=text,
                        start_index=next_index,
                        tokens=tokens,
                    )
                    params, body_tokens = function_defines[text]
                    expanded = _expand_function_macro(
                        body_tokens=body_tokens,
                        call_line=line,
                        invocation_args=arguments,
                        name=text,
                        params=params,
                    )
                    result.extend(expanded)
                    changed = True
                    index = after
                    continue
                # Function-like macro name without a following ``(``: leave
                # the IDENT alone (matches C; a bare WEXITSTATUS isn't an
                # invocation, even if the same name is #define'd).
                result.append(tokens[index])
                index += 1
                continue
            if kind == "IDENT" and text in defines:
                value_tokens = tokenize(defines[text])
                for value_kind, value_text, _drop in value_tokens[:-1]:  # drop trailing EOF
                    result.append((value_kind, value_text, line))
                changed = True
                index += 1
                continue
            result.append(tokens[index])
            index += 1
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
    search_paths: tuple[Path, ...] = (),
) -> tuple[
    str,
    dict[str, str],
    dict[str, tuple[tuple[str, ...], list[tuple[str, str, int]]]],
]:
    """Expand ``#include "..."`` directives and collect ``#define`` macros.

    Both object-like (``#define NAME VALUE``) and function-like
    (``#define NAME(p1, p2) BODY``) macros are recognised.  The shape
    is decided by whether ``(`` immediately follows the macro name
    with no intervening whitespace; the C standard treats
    ``#define FOO(x) ...`` and ``#define FOO (x) ...`` as different
    directives (function-like vs. object-like whose value happens to
    start with a paren).  Each ``#define`` line is replaced with a
    blank line so downstream line numbers stay correct.

    ``#include`` accepts only the double-quoted form.  The path is
    resolved against *include_base* first (the directory of the source
    currently being preprocessed — matching NASM's ``%include``); if
    not found, each entry in *search_paths* is tried in order.
    Included files are preprocessed recursively; their ``#define``
    entries merge into the outer pool so later definitions override.
    ``include_stack`` carries the set of files currently being
    expanded so a cycle is rejected with a clear error.  The directive
    line itself is replaced by the included file's processed text, so
    error line numbers after an include shift by the included file's
    length — acceptable in the absence of ``#line`` support.

    Returns:
        (processed_source, defines, function_defines).  ``defines``
        maps each object-like macro name to its raw value text, which
        is retokenized at substitution time so the tokens inherit the
        current position's line number.  ``function_defines`` maps
        each function-like macro name to ``(params, body_tokens)``;
        body tokens are pre-tokenized at define time and re-stamped
        with the call-site line at expansion time.

    """
    if include_base is None:
        include_base = Path()
    defines: dict[str, str] = {}
    function_defines: dict[str, tuple[tuple[str, ...], list[tuple[str, str, int]]]] = {}
    output_lines: list[str] = []
    # Conditional-compilation stack.  Each entry is ``(name, skipping,
    # opened_line)``: ``skipping`` is True if the ``#ifndef NAME`` block
    # should drop its body (i.e. ``NAME`` was already defined at the
    # directive's line), False otherwise.  Nested ``#ifndef`` adds a new
    # frame; ``#endif`` pops the top frame.  ``opened_line`` is recorded
    # so an unterminated-block diagnostic can point at the offending
    # directive.  A line is emitted only if every frame on the stack
    # has ``skipping=False``.
    ifndef_stack: list[tuple[str, bool, int]] = []
    for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
        stripped = line.lstrip()
        # ``#endif`` always pops, even from inside a skipping block.
        # Handle this before the "currently skipping" gate so nested
        # ``#ifndef`` / ``#endif`` pairs inside a skipped block still
        # balance correctly.
        if stripped.startswith("#endif"):
            if not ifndef_stack:
                message = "#endif without matching #ifndef"
                raise CompileError(message, line=line_number)
            ifndef_stack.pop()
            output_lines.append("\n")  # Preserve line numbering.
            continue
        if stripped.startswith("#ifndef"):
            name_text = stripped[len("#ifndef") :].strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name_text):
                message = f"malformed #ifndef: {line.rstrip()!r}"
                raise CompileError(message, line=line_number)
            # An inner ``#ifndef`` nested inside a skipping block stays
            # in "skipping" mode regardless of whether its own name is
            # defined — the body never runs, so it makes no difference.
            already_skipping = any(skipping for _name, skipping, _opened in ifndef_stack)
            name_is_defined = name_text in defines or name_text in function_defines
            ifndef_stack.append((name_text, already_skipping or name_is_defined, line_number))
            output_lines.append("\n")  # Preserve line numbering.
            continue
        if any(skipping for _name, skipping, _opened in ifndef_stack):
            # Currently inside an ``#ifndef`` block whose guard name was
            # already defined: drop the line entirely (but keep newline
            # accounting so post-block line numbers stay aligned).
            output_lines.append("\n")
            continue
        if stripped.startswith("#include"):
            match = INCLUDE_PATTERN.match(stripped[len("#include") :])
            if match is None:
                message = f"malformed #include: {line.rstrip()!r}"
                raise CompileError(message, line=line_number)
            include_name = match.group(1)
            candidates = [include_base, *search_paths]
            include_path = None
            for candidate_base in candidates:
                candidate = (candidate_base / include_name).resolve()
                if candidate.is_file():
                    include_path = candidate
                    break
            if include_path is None:
                message = f"cannot open #include file {include_name!r}: not found in {[str(c) for c in candidates]}"
                raise CompileError(message, line=line_number)
            if include_path in include_stack:
                message = f"circular #include of {include_path}"
                raise CompileError(message, line=line_number)
            try:
                included_source = include_path.read_text(encoding="utf-8")
            except OSError as error:
                message = f"cannot open #include file {include_name!r}: {error}"
                raise CompileError(message, line=line_number) from error
            included_text, included_defines, included_function_defines = preprocess(
                included_source,
                include_base=include_path.parent,
                include_stack=include_stack | {include_path},
                search_paths=search_paths,
            )
            defines.update(included_defines)
            function_defines.update(included_function_defines)
            output_lines.append(included_text)
            continue
        if not stripped.startswith("#define"):
            output_lines.append(line)
            continue
        # Strip ``#define`` and inspect what follows.  C distinguishes
        # function-like from object-like by whether ``(`` immediately
        # touches the macro name with **no whitespace** between them;
        # ``#define FOO(x) ...`` is function-like, but
        # ``#define FOO (x) ...`` is an object-like macro whose value
        # is the token sequence ``(x) ...``.  The regex captures the
        # name and verifies the very next character is ``(``.
        remainder = stripped[len("#define") :]
        function_match = FUNCTION_DEFINE_PATTERN.match(remainder)
        if function_match is not None and remainder[function_match.end("name") : function_match.end("name") + 1] == "(":
            name = function_match.group("name")
            params, body_tokens = _parse_function_define(
                body_text=function_match.group("body"),
                line_number=line_number,
                name=name,
                params_text=function_match.group("params"),
            )
            function_defines[name] = (params, body_tokens)
            output_lines.append("\n")  # Preserve line numbering.
            continue
        parts = stripped.split(None, 2)
        # ``#define NAME`` (no value) is the header-guard shape: define
        # NAME as an empty object-like macro so a subsequent
        # ``#ifndef NAME`` sees it as defined and skips.  Substitution
        # of the empty value emits no tokens (the empty string lexes
        # to just ``EOF``, which apply_defines already drops).
        if len(parts) == 2:
            name = parts[1]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
                message = f"malformed #define: {line.rstrip()!r}"
                raise CompileError(message, line=line_number)
            defines[name] = ""
            output_lines.append("\n")  # Preserve line numbering.
            continue
        if len(parts) < 3:
            message = f"malformed #define: {line.rstrip()!r}"
            raise CompileError(message, line=line_number)
        name = parts[1]
        value = parts[2].rstrip()
        if not value:
            # Whitespace-only value collapses to empty; treat exactly
            # like the bare-name form rather than a hard error.  This
            # keeps ``#define FOO   `` working as a header-guard token.
            defines[name] = ""
            output_lines.append("\n")  # Preserve line numbering.
            continue
        defines[name] = value
        output_lines.append("\n")  # Preserve line numbering.
    if ifndef_stack:
        name, _skipping, opened_line = ifndef_stack[-1]
        message = f"unterminated #ifndef {name!r} (no matching #endif)"
        raise CompileError(message, line=opened_line)
    return "".join(output_lines), defines, function_defines
