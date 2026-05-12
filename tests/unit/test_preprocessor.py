"""Pytest unit tests for cc.preprocessor's macro and conditional support.

Drives :func:`cc.preprocessor.preprocess` + :func:`cc.lexer.tokenize` +
:func:`cc.preprocessor.apply_defines` directly (no codegen) and asserts
on the resulting token stream — sufficient to pin substitution
behaviour without depending on parser/codegen details.

Covers three feature areas:

* Object-like macros (``#define NAME VALUE`` and the bare-name
  ``#define NAME`` header-guard form).
* Function-like macros (``#define NAME(args) BODY``).
* Header-guard conditionals (``#ifndef`` / ``#endif``), including
  nesting and error paths.

Limitations the tests deliberately encode:

* Stringification (``#``) and token pasting (``##``) are not supported.
* Variadic macros (``...`` / ``__VA_ARGS__``) are not supported.
* ``#ifdef``, ``#if``, ``#else``, ``#elif``, ``#undef`` are not
  supported.

Run with: ``pytest tests/unit/test_preprocessor.py``
"""

from __future__ import annotations

import pytest

from cc.errors import CompileError
from cc.lexer import tokenize
from cc.preprocessor import apply_defines, preprocess


def _expand(source: str) -> list[tuple[str, str]]:
    """Run the full preprocessor + lexer + macro substitution pipeline.

    Returns the post-expansion token stream as ``(kind, text)`` pairs
    (line numbers dropped — they're verified separately).  The
    trailing ``EOF`` token is dropped so test assertions stay focused
    on the meaningful tokens.
    """
    processed, defines, function_defines = preprocess(source)
    tokens = tokenize(processed)
    expanded = apply_defines(defines=defines, function_defines=function_defines, tokens=tokens)
    return [(kind, text) for kind, text, _line in expanded if kind != "EOF"]


def test_argument_is_itself_a_macro_call() -> None:
    """Arguments that are themselves macro calls expand after substitution."""
    source = "#define INC(x) ((x) + 1)\nint r = INC(INC(2));\n"
    tokens = _expand(source)
    numbers = [text for kind, text in tokens if kind == "NUMBER"]
    # The expansion is ``( ( ( 2 ) + 1 ) + 1 )`` — two ``1``s and one ``2``.
    assert numbers.count("2") == 1
    assert numbers.count("1") == 2


def test_argument_with_nested_paren_commas() -> None:
    """Commas inside nested parens belong to the current argument, not the splitter."""
    source = "#define PICK(a, b) (a)\nint v = PICK(foo(x, y), z);\n"
    tokens = _expand(source)
    # The first argument is ``foo(x, y)`` (note: contains a comma at depth 1).
    # The body ``(a)`` selects it, so we expect ``( foo ( x , y ) ) ;``.
    text_stream = [text for _kind, text in tokens if text]
    assert "foo" in text_stream
    assert "x" in text_stream
    assert "y" in text_stream
    # ``z`` is the discarded second argument and must not leak through.
    assert "z" not in text_stream


def test_bare_define_without_value_is_defined() -> None:
    """``#define NAME`` with no value defines NAME (visible to a later ``#ifndef``).

    The bare-name shape is the header-guard idiom: once ``#define
    FOO_H`` runs, ``#ifndef FOO_H`` afterward must treat the name as
    defined and skip its body.  Same source-file flow exercises this
    without needing the include machinery.
    """
    source = "#define FOO\n#ifndef FOO\nint dropped = 1;\n#endif\nint kept = 2;\n"
    tokens = _expand(source)
    text_stream = [text for _kind, text in tokens]
    # ``dropped`` should be absent; ``kept`` should remain.
    assert "dropped" not in text_stream
    assert "kept" in text_stream


def test_duplicate_parameter_raises() -> None:
    """A macro with the same parameter twice raises CompileError at parse time."""
    source = "#define BAD(x, x) ((x))\n"
    with pytest.raises(CompileError, match="duplicate"):
        preprocess(source)


def test_expansion_inherits_use_site_line_number() -> None:
    """Substituted tokens carry the call-site line, not the define-site line."""
    source = "#define DOUBLE(x) ((x) + (x))\n\n\nint y = DOUBLE(7);\n"
    processed, defines, function_defines = preprocess(source)
    tokens = tokenize(processed)
    expanded = apply_defines(defines=defines, function_defines=function_defines, tokens=tokens)
    # ``DOUBLE(7)`` is on line 4 of the original source (blank lines
    # preserved at the #define site, plus two literal blank lines).
    seven_lines = [line for kind, text, line in expanded if kind == "NUMBER" and text == "7"]
    assert seven_lines and all(line == 4 for line in seven_lines)


def test_function_like_macro_basic() -> None:
    """A simple ``DOUBLE(x)`` macro substitutes its argument into the body."""
    source = "#define DOUBLE(x) ((x) + (x))\nint y = DOUBLE(7);\n"
    tokens = _expand(source)
    # Expect: int y = ( ( 7 ) + ( 7 ) ) ;
    assert tokens == [
        ("INT", "int"),
        ("IDENT", "y"),
        ("ASSIGN", "="),
        ("LPAREN", "("),
        ("LPAREN", "("),
        ("NUMBER", "7"),
        ("RPAREN", ")"),
        ("PLUS", "+"),
        ("LPAREN", "("),
        ("NUMBER", "7"),
        ("RPAREN", ")"),
        ("RPAREN", ")"),
        ("SEMI", ";"),
    ]


def test_function_like_macro_multiple_parameters() -> None:
    """A two-parameter macro substitutes each parameter independently."""
    source = "#define ADD(a, b) ((a) + (b))\nint z = ADD(3, 4);\n"
    tokens = _expand(source)
    assert ("NUMBER", "3") in tokens
    assert ("NUMBER", "4") in tokens
    # No leftover parameter identifiers.
    assert ("IDENT", "a") not in tokens
    assert ("IDENT", "b") not in tokens


def test_function_like_name_without_paren_not_expanded() -> None:
    """Bare ``MACRO`` (no following ``(``) is left as a plain identifier.

    Function-like macros only fire when followed by an open paren;
    using the bare name elsewhere (rare but legal) must not consume
    surrounding tokens.
    """
    source = "#define FOO(x) ((x) + 1)\nint *p = &FOO;\n"
    tokens = _expand(source)
    # ``FOO`` survives as an IDENT because no ``(`` follows.
    assert ("IDENT", "FOO") in tokens


def test_header_guard_basic_includes_body() -> None:
    """The canonical ``#ifndef``/``#define``/``#endif`` block keeps its body.

    First-time encounter: the guard name is not yet defined, so the
    block runs normally — the ``#define`` registers the guard and the
    body tokens flow through.
    """
    source = "#ifndef FOO_H\n#define FOO_H\nint body_token;\n#endif\n"
    processed, defines, _function_defines = preprocess(source)
    assert "FOO_H" in defines
    assert not defines["FOO_H"]  # bare-name #define -> empty value
    tokens = tokenize(processed)
    text_stream = [text for _kind, text, _line in tokens]
    assert "body_token" in text_stream


def test_header_guard_second_pass_skips_body() -> None:
    """A second ``#ifndef`` for the same guard name in one source skips its body.

    Once the guard's ``#define`` has fired, a follow-up ``#ifndef``
    for the same name must drop everything up to its ``#endif`` —
    this is the mechanic that makes header guards prevent double
    inclusion (modelled here within one source file because each
    ``#include`` in cc.py preprocesses independently).
    """
    source = "#ifndef FOO_H\n#define FOO_H\nint first_pass;\n#endif\n#ifndef FOO_H\nint second_pass;\n#endif\n"
    tokens = _expand(source)
    text_stream = [text for _kind, text in tokens]
    assert "first_pass" in text_stream
    assert "second_pass" not in text_stream


def test_ifndef_function_like_macro_counts_as_defined() -> None:
    """``#ifndef NAME`` treats a function-like macro NAME as defined.

    Function-like and object-like macros live in separate dicts but
    share a single namespace for the purposes of ``#ifndef`` — a name
    defined either way must trip the guard.
    """
    source = "#define WEXITSTATUS(s) (((s) >> 8) & 0xFF)\n#ifndef WEXITSTATUS\nint dropped;\n#endif\nint kept;\n"
    tokens = _expand(source)
    text_stream = [text for _kind, text in tokens]
    assert "dropped" not in text_stream
    assert "kept" in text_stream


def test_ifndef_mismatched_endif_raises() -> None:
    """A stray ``#endif`` with no matching ``#ifndef`` is a hard error."""
    source = "int x;\n#endif\n"
    with pytest.raises(CompileError, match="#endif"):
        preprocess(source)


def test_ifndef_nested_blocks() -> None:
    """Nested ``#ifndef`` blocks each track their own guard state.

    The outer guard is undefined so its body runs; the inner guard is
    also undefined so its body runs too.  After the inner ``#endif``
    we're back in the outer block (still active).  A second top-level
    ``#ifndef`` over the inner guard's name then skips, proving the
    inner ``#define`` registered correctly even when nested.
    """
    source = (
        "#ifndef OUTER\n"
        "#define OUTER\n"
        "int outer_body;\n"
        "#ifndef INNER\n"
        "#define INNER\n"
        "int inner_body;\n"
        "#endif\n"
        "int after_inner;\n"
        "#endif\n"
        "#ifndef INNER\n"
        "int should_be_dropped;\n"
        "#endif\n"
    )
    tokens = _expand(source)
    text_stream = [text for _kind, text in tokens]
    assert "outer_body" in text_stream
    assert "inner_body" in text_stream
    assert "after_inner" in text_stream
    assert "should_be_dropped" not in text_stream


def test_ifndef_unterminated_raises() -> None:
    """An ``#ifndef`` with no matching ``#endif`` before EOF is a hard error."""
    source = "#ifndef FOO_H\nint body;\n"
    with pytest.raises(CompileError, match="unterminated"):
        preprocess(source)


def test_nested_function_like_expansion() -> None:
    """A macro body that calls another macro is fully expanded in one pass."""
    source = "#define A(x) B(x)\n#define B(x) ((x) + 1)\nint q = A(5);\n"
    tokens = _expand(source)
    numbers = [text for kind, text in tokens if kind == "NUMBER"]
    assert "5" in numbers
    assert "1" in numbers
    # No leftover ``A`` / ``B`` invocations.
    assert ("IDENT", "A") not in tokens
    assert ("IDENT", "B") not in tokens


def test_object_and_function_like_coexist() -> None:
    """Object-like and function-like macros do not interfere."""
    source = "#define N 5\n#define DOUBLE(x) ((x) * 2)\nint v = DOUBLE(N);\n"
    tokens = _expand(source)
    # ``N`` expands to 5 inside DOUBLE's substituted body.
    text_stream = [text for kind, text in tokens if kind == "NUMBER"]
    assert "5" in text_stream
    assert "2" in text_stream


def test_object_like_macro_unchanged() -> None:
    """Object-like macros still expand exactly like the prior implementation."""
    source = "#define N 5\nint x = N;\n"
    tokens = _expand(source)
    assert ("NUMBER", "5") in tokens
    assert ("IDENT", "N") not in tokens


def test_object_like_with_parens_in_value_is_not_function_like() -> None:
    """``#define FOO (x)`` (space before paren) is object-like, not function-like.

    C's rule: the open paren must touch the macro name with no
    whitespace.  ``#define FOO (x)`` defines an object-like macro
    whose value is the token sequence ``(x)``; ``FOO`` then expands
    to ``( x )`` everywhere, with no argument-list parsing.
    """
    source = "#define FOO (1+2)\nint v = FOO;\n"
    tokens = _expand(source)
    # No FOO IDENT left; substitution produced parenthesized literal.
    assert ("IDENT", "FOO") not in tokens
    assert ("LPAREN", "(") in tokens
    assert ("NUMBER", "1") in tokens
    assert ("PLUS", "+") in tokens
    assert ("NUMBER", "2") in tokens


def test_repeated_invocation_in_one_expression() -> None:
    """The same macro can be invoked more than once on a single line."""
    source = "#define SQ(x) ((x) * (x))\nint w = SQ(3) + SQ(4);\n"
    tokens = _expand(source)
    numbers = [text for kind, text in tokens if kind == "NUMBER"]
    # Two ``3``s (one per occurrence of ``x`` in SQ(3)) and two ``4``s.
    assert numbers.count("3") == 2
    assert numbers.count("4") == 2


def test_unterminated_invocation_raises() -> None:
    """An unmatched ``(`` after a macro name raises CompileError."""
    source = "#define FOO(x) (x)\nint y = FOO(1\n"
    with pytest.raises(CompileError, match="FOO"):
        _expand(source)


def test_wrong_argument_count_raises() -> None:
    """Calling a function-like macro with the wrong arity raises CompileError."""
    source = "#define ADD(a, b) ((a) + (b))\nint x = ADD(1);\n"
    with pytest.raises(CompileError, match="ADD"):
        _expand(source)


def test_zero_parameter_function_like() -> None:
    """``MACRO()`` with no parameters is accepted and expands its body."""
    source = "#define PI() 314\nint a = PI();\n"
    tokens = _expand(source)
    assert ("NUMBER", "314") in tokens
