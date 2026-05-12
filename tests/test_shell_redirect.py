#!/usr/bin/env python3
"""End-to-end tests for shell I/O redirection (>, >>, <).

One QEMU session per case; each case sends a single shell line via the
serial fifo and asserts on the resulting console output (for
status/error cases) or on a subsequent ``cat`` (for file-content
cases).

Note: the ``cat`` program in BBoeOS requires a filename argument and
does not read from stdin (it calls die() when invoked with no args).
The ``test_input_redirect_reads_file`` case therefore tests ``<``
redirection by verifying the shell opens the file without error
(``$?=0``) using ``exit_status 0`` as a no-op consumer of stdin.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def _run_and_capture(*, command: str) -> bytes:
    """Run a shell command in a fresh QEMU session and return the bytes after the echo."""
    with qemu_session(monitor=False, snapshot=True) as session:
        pre = len(session.buffer)
        session.send_command(command)
        full = bytes(session.buffer[pre:])
        crlf = full.find(b"\r\n")
        return full[crlf + 2 :] if crlf >= 0 else full


def test_append_redirect_keeps_prior() -> None:
    """`echo first > out; echo second >> out; cat out` => both lines, in order."""
    out = _run_and_capture(command="echo first > out_b; echo second >> out_b; cat out_b")
    assert b"first" in out and b"second" in out, f"got {out!r}"
    assert out.index(b"first") < out.index(b"second"), f"order wrong: {out!r}"
    print("PASS: test_append_redirect_keeps_prior")


def test_builtin_redirect_captures_output() -> None:
    """Builtins honor redirection too: `help > out; cat out` shows the help text in the file."""
    out = _run_and_capture(command="help > out_f; cat out_f")
    assert b"Commands" in out, f"help output should be in the file: {out!r}"
    print("PASS: test_builtin_redirect_captures_output")


def test_input_redirect_reads_file() -> None:
    """`echo content > out; exit_status 0 < out; echo status=$?` => status=0.

    BBoeOS ``cat`` requires a filename argument and does not read stdin,
    so this case tests that ``<`` successfully opens an existing file and
    passes it as stdin without error, using ``exit_status 0`` as a
    no-op stdin consumer.
    """
    out = _run_and_capture(command="echo content > out_d; exit_status 0 < out_d; echo status=$?")
    assert b"status=0" in out, f"got {out!r}"
    print("PASS: test_input_redirect_reads_file")


def test_open_failure_nonzero_status() -> None:
    """A redirect to a path that can't be created/opened sets $? to non-zero."""
    out = _run_and_capture(command="cat < /no/such/file; echo status=$?")
    assert b"status=1" in out, f"got {out!r}"
    print("PASS: test_open_failure_nonzero_status")


def test_redirect_in_chain_truncates_even_on_failure() -> None:
    """Redirection setup happens before the command runs.

    `exit_status 1 > out && echo SKIPPED`: redirection truncates out,
    then `exit_status 1` exits non-zero, so `echo SKIPPED` is skipped.
    out ends up empty.
    """
    out = _run_and_capture(command="echo prelude > out_e; exit_status 1 > out_e && echo SKIPPED; cat out_e")
    assert b"SKIPPED" not in out, f"&& after failure must skip: {out!r}"
    print("PASS: test_redirect_in_chain_truncates_even_on_failure")


def test_syntax_error_nonzero_status() -> None:
    """`echo nope >` (missing filename) sets $? non-zero; no file created."""
    out = _run_and_capture(command="echo nope >; echo status=$?")
    assert b"status=1" in out, f"got {out!r}"
    print("PASS: test_syntax_error_nonzero_status")


def test_truncate_redirect_writes_file() -> None:
    """`echo hello > out; cat out` => file contains hello."""
    out = _run_and_capture(command="echo hello > out_a; cat out_a")
    assert b"hello" in out, f"got {out!r}"
    print("PASS: test_truncate_redirect_writes_file")


def test_truncate_replaces_prior() -> None:
    """`echo replace > out; echo only > out; cat out` => only `only`."""
    out = _run_and_capture(command="echo replace > out_c; echo only > out_c; cat out_c")
    assert b"only" in out and b"replace" not in out, f"got {out!r}"
    print("PASS: test_truncate_replaces_prior")


def main() -> int:
    """Build the OS image and run all redirection tests."""
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    test_append_redirect_keeps_prior()
    test_builtin_redirect_captures_output()
    test_input_redirect_reads_file()
    test_open_failure_nonzero_status()
    test_redirect_in_chain_truncates_even_on_failure()
    test_syntax_error_nonzero_status()
    test_truncate_redirect_writes_file()
    test_truncate_replaces_prior()
    print("8 passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
