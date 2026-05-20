"""Pytest unit tests for the shell command-line lexer.

The lexer (``user/libbboeos/include/shell_lex.h``) is a single-pass, quote-aware
tokenizer that the bboeos shell uses to break a command line into a
TOKEN_* stream before any parser/executor work.  This test compiles
the header against a tiny C harness with host clang and asserts the
emitted token sequence and word bytes for each input — no QEMU
involvement, runs in well under a second.

Run with: ``pytest tests/unit/test_shell_lex.py``
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INCLUDE_DIR = REPO_ROOT / "user" / "libbboeos" / "include"

_HARNESS = r"""
#include <stdio.h>
#include <string.h>

#include "shell_lex.h"

#define MAX_TOKENS 64
#define MAX_WORD_BYTES 512

/* enum-typed parameter so clang's -Wswitch (escalated by -Werror)
   trips if a new TokenKind variant is added without an arm here. */
static const char *kind_name(enum TokenKind kind) {
    switch (kind) {
    case TOKEN_AND:             return "AND";
    case TOKEN_EOF:             return "EOF";
    case TOKEN_OR:              return "OR";
    case TOKEN_PIPE:            return "PIPE";
    case TOKEN_REDIRECT_APPEND: return "REDIRECT_APPEND";
    case TOKEN_REDIRECT_IN:     return "REDIRECT_IN";
    case TOKEN_REDIRECT_OUT:    return "REDIRECT_OUT";
    case TOKEN_SEMI:            return "SEMI";
    case TOKEN_WORD:            return "WORD";
    }
    return "?";
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: lex_harness <line>\n");
        return 2;
    }
    /* Input line is larger than the lex output buffers so we can
       exercise the overflow path with a single big argv. */
    char line[4096];
    strncpy(line, argv[1], sizeof(line) - 1);
    line[sizeof(line) - 1] = '\0';
    int token_kinds[MAX_TOKENS];
    int token_word_offsets[MAX_TOKENS];
    char word_buffer[MAX_WORD_BYTES];
    int count = lex_line(line, token_kinds, token_word_offsets,
                         word_buffer, MAX_TOKENS, MAX_WORD_BYTES);
    if (count < 0) {
        printf("OVERFLOW\n");
        return 0;
    }
    printf("COUNT %d\n", count);
    for (int i = 0; i < count; i += 1) {
        if (token_kinds[i] == TOKEN_WORD) {
            printf("%s %s\n", kind_name(token_kinds[i]),
                   word_buffer + token_word_offsets[i]);
        } else {
            printf("%s\n", kind_name(token_kinds[i]));
        }
    }
    printf("%s\n", kind_name(token_kinds[count]));
    return 0;
}
"""


def _run_lex(line: str) -> list[str]:
    """Compile the harness, lex *line*, return stdout lines."""
    with tempfile.TemporaryDirectory(prefix="test_shell_lex_") as work:
        work_path = Path(work)
        source = work_path / "harness.c"
        binary = work_path / "harness"
        source.write_text(_HARNESS)
        subprocess.run(
            ["clang", "-std=c99", "-Wall", "-Werror", f"-I{INCLUDE_DIR}", str(source), "-o", str(binary)],
            capture_output=True,
            check=True,
            text=True,
        )
        result = subprocess.run(
            [str(binary), line],
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout.splitlines()


def test_and_operator() -> None:
    """``&&`` emits a single AND token."""
    assert _run_lex("a && b") == [
        "COUNT 3",
        "WORD a",
        "AND",
        "WORD b",
        "EOF",
    ]


def test_double_quoted_word_preserves_spaces() -> None:
    """Spaces inside ``"…"`` stay inside the same WORD."""
    assert _run_lex('echo "hello world"') == [
        "COUNT 2",
        "WORD echo",
        "WORD hello world",
        "EOF",
    ]


def test_double_quotes_preserve_single_quote() -> None:
    """A literal ``'`` inside double quotes is kept as part of the word."""
    assert _run_lex("""echo "a'b" """) == [
        "COUNT 2",
        "WORD echo",
        "WORD a'b",
        "EOF",
    ]


def test_empty_input() -> None:
    """An empty line lexes to zero tokens plus the EOF terminator."""
    assert _run_lex("") == ["COUNT 0", "EOF"]


def test_full_color_command_with_semicolons() -> None:
    """The motivating regression: 256-color SGR with semicolons inside quotes.

    Previously the shell's chain parser split this line on every ``;``,
    producing 7 "unknown command" lines.  Under the new lexer the entire
    SGR payload stays in a single WORD token attached to ``-e``.
    """
    assert _run_lex(r"""echo -e '\e[38;5;196mred\e[38;5;46m green\e[38;5;21m blue\e[0m'""") == [
        "COUNT 3",
        "WORD echo",
        "WORD -e",
        r"WORD \e[38;5;196mred\e[38;5;46m green\e[38;5;21m blue\e[0m",
        "EOF",
    ]


def test_mixed_chain_and_pipe() -> None:
    """A realistic mixed line lexes into the expected operator sequence."""
    assert _run_lex("echo a | grep a && echo ok ; echo done") == [
        "COUNT 11",
        "WORD echo",
        "WORD a",
        "PIPE",
        "WORD grep",
        "WORD a",
        "AND",
        "WORD echo",
        "WORD ok",
        "SEMI",
        "WORD echo",
        "WORD done",
        "EOF",
    ]


def test_multiple_words() -> None:
    """Whitespace-separated barewords each emit their own WORD."""
    assert _run_lex("echo hello world") == [
        "COUNT 3",
        "WORD echo",
        "WORD hello",
        "WORD world",
        "EOF",
    ]


def test_no_whitespace_around_operators() -> None:
    """Operators do not require surrounding whitespace."""
    assert _run_lex("a;b&&c||d") == [
        "COUNT 7",
        "WORD a",
        "SEMI",
        "WORD b",
        "AND",
        "WORD c",
        "OR",
        "WORD d",
        "EOF",
    ]


def test_only_whitespace() -> None:
    """Leading/trailing whitespace produces no tokens."""
    assert _run_lex("   \t  ") == ["COUNT 0", "EOF"]


def test_or_operator() -> None:
    """``||`` emits a single OR token."""
    assert _run_lex("a || b") == [
        "COUNT 3",
        "WORD a",
        "OR",
        "WORD b",
        "EOF",
    ]


def test_overflow_returns_negative() -> None:
    """Exceeding max_word_bytes flags overflow."""
    # 600 'a's exceeds the harness's MAX_WORD_BYTES = 512.
    lines = _run_lex("a" * 600)
    assert lines == ["OVERFLOW"]


def test_pipe_operator() -> None:
    """A single ``|`` emits PIPE (not OR)."""
    assert _run_lex("a | b") == [
        "COUNT 3",
        "WORD a",
        "PIPE",
        "WORD b",
        "EOF",
    ]


def test_quoted_and_stays_in_word() -> None:
    """``&&`` inside quotes does not emit AND."""
    assert _run_lex("echo 'a && b'") == [
        "COUNT 2",
        "WORD echo",
        "WORD a && b",
        "EOF",
    ]


def test_quoted_pipe_stays_in_word() -> None:
    """``|`` inside quotes does not emit PIPE."""
    assert _run_lex("echo 'a|b'") == [
        "COUNT 2",
        "WORD echo",
        "WORD a|b",
        "EOF",
    ]


def test_quoted_redirect_stays_in_word() -> None:
    """``>`` and ``<`` inside quotes stay in the WORD."""
    assert _run_lex("echo '<x>'") == [
        "COUNT 2",
        "WORD echo",
        "WORD <x>",
        "EOF",
    ]


def test_quoted_semicolon_stays_in_word() -> None:
    """``;`` inside quotes is folded into the surrounding WORD."""
    assert _run_lex("echo 'a;b'") == [
        "COUNT 2",
        "WORD echo",
        "WORD a;b",
        "EOF",
    ]


def test_redirect_append_operator() -> None:
    """``>>`` emits REDIRECT_APPEND, not two REDIRECT_OUT tokens."""
    assert _run_lex("echo hi >> log") == [
        "COUNT 4",
        "WORD echo",
        "WORD hi",
        "REDIRECT_APPEND",
        "WORD log",
        "EOF",
    ]


def test_redirect_in_operator() -> None:
    """``<`` emits REDIRECT_IN; following word is a separate token."""
    assert _run_lex("cat < file.txt") == [
        "COUNT 3",
        "WORD cat",
        "REDIRECT_IN",
        "WORD file.txt",
        "EOF",
    ]


def test_redirect_out_operator() -> None:
    """``>`` emits REDIRECT_OUT."""
    assert _run_lex("echo hi > out") == [
        "COUNT 4",
        "WORD echo",
        "WORD hi",
        "REDIRECT_OUT",
        "WORD out",
        "EOF",
    ]


def test_semicolon_emits_operator() -> None:
    """An unquoted ``;`` between words emits a SEMI token."""
    assert _run_lex("a ; b") == [
        "COUNT 3",
        "WORD a",
        "SEMI",
        "WORD b",
        "EOF",
    ]


def test_single_quoted_word_preserves_spaces() -> None:
    """Spaces inside ``'…'`` stay inside the same WORD."""
    assert _run_lex("echo 'hello world'") == [
        "COUNT 2",
        "WORD echo",
        "WORD hello world",
        "EOF",
    ]


def test_single_quotes_preserve_double_quote() -> None:
    """A literal ``"`` inside single quotes is kept as part of the word."""
    assert _run_lex("""echo 'a"b'""") == [
        "COUNT 2",
        "WORD echo",
        'WORD a"b',
        "EOF",
    ]


def test_single_word() -> None:
    """A bare bareword emits one WORD token."""
    assert _run_lex("echo") == ["COUNT 1", "WORD echo", "EOF"]
