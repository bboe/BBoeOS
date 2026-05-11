#!/usr/bin/env python3
"""Shell command-chaining smoke test.

Exercises `;`, `&&`, and `||` separators in the shell's dispatch loop:
each line tokenizes into segments whose execution depends on the
previous segment's exit status.  Mirrors the test_shell_history.py
style — single QEMU boot per case, serial fifo, prompt-driven sync.

Run standalone:
    tests/test_shell_chain.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def assert_in(*, expected: bytes, haystack: bytes, label: str) -> None:
    """Raise AssertionError if *expected* is not found in *haystack*."""
    if expected not in haystack:
        message = f"{label}: expected {expected!r} in {haystack!r}"
        raise AssertionError(message)


def assert_not_in(*, needle: bytes, haystack: bytes, label: str) -> None:
    """Raise AssertionError if *needle* is found in *haystack*."""
    if needle in haystack:
        message = f"{label}: unexpected {needle!r} in {haystack!r}"
        raise AssertionError(message)


def output_after_command(*, session: object, pre_length: int) -> bytes:
    r"""Return the bytes that appear after the typed command line.

    The serial buffer contains the line editor's character-by-character
    echo of the command (which would false-positive a substring search
    for echo arguments), then `\r\n`, then any program output, then
    the next prompt.  Slice off everything up to and including the
    first `\r\n` after pre_length so callers see only program output.
    """
    full = bytes(session.buffer[pre_length:])
    crlf = full.find(b"\r\n")
    if crlf < 0:
        return full
    return full[crlf + 2 :]


def test_and_runs_on_success() -> None:
    """`exit_status 0 && echo ran`: second segment runs because exit_status 0 exits 0."""
    with qemu_session(monitor=False, snapshot=True) as session:
        pre_length = len(session.buffer)
        session.send_command("exit_status 0 && echo and_ok")
        between = output_after_command(session=session, pre_length=pre_length)
        assert_in(expected=b"and_ok", haystack=between, label="&& after success must run RHS")
    print("PASS: test_and_runs_on_success")


def test_and_skips_on_failure() -> None:
    """`exit_status 1 && echo skip`: second segment is skipped because exit_status 1 exits non-zero."""
    with qemu_session(monitor=False, snapshot=True) as session:
        pre_length = len(session.buffer)
        session.send_command("exit_status 1 && echo and_ran")
        between = output_after_command(session=session, pre_length=pre_length)
        assert_not_in(needle=b"and_ran", haystack=between, label="&& after failure must skip RHS")
    print("PASS: test_and_skips_on_failure")


def test_dollar_question_between_segments() -> None:
    """`exit_status 1; echo $?` sees the freshly-updated exit status (1, not stale)."""
    with qemu_session(monitor=False, snapshot=True) as session:
        pre_length = len(session.buffer)
        session.send_command("exit_status 1; echo $?")
        between = output_after_command(session=session, pre_length=pre_length)
        assert_in(expected=b"1", haystack=between, label="$? after `exit_status 1` should be 1")
    print("PASS: test_dollar_question_between_segments")


def test_mixed_chain_left_associative() -> None:
    """`exit_status 1 || echo a && echo b` runs both: || picks up failure, && picks up echo's success."""
    with qemu_session(monitor=False, snapshot=True) as session:
        pre_length = len(session.buffer)
        session.send_command("exit_status 1 || echo or_a && echo and_b")
        between = output_after_command(session=session, pre_length=pre_length)
        assert_in(expected=b"or_a", haystack=between, label="|| after failure must run RHS")
        assert_in(expected=b"and_b", haystack=between, label="&& after echo success must run RHS")
    print("PASS: test_mixed_chain_left_associative")


def test_or_runs_on_failure() -> None:
    """`exit_status 1 || echo rescue`: second segment runs because exit_status 1 exits non-zero."""
    with qemu_session(monitor=False, snapshot=True) as session:
        pre_length = len(session.buffer)
        session.send_command("exit_status 1 || echo or_ran")
        between = output_after_command(session=session, pre_length=pre_length)
        assert_in(expected=b"or_ran", haystack=between, label="|| after failure must run RHS")
    print("PASS: test_or_runs_on_failure")


def test_or_skips_on_success() -> None:
    """`exit_status 0 || echo nope`: second segment is skipped because exit_status 0 exits 0."""
    with qemu_session(monitor=False, snapshot=True) as session:
        pre_length = len(session.buffer)
        session.send_command("exit_status 0 || echo or_skipped")
        between = output_after_command(session=session, pre_length=pre_length)
        assert_not_in(needle=b"or_skipped", haystack=between, label="|| after success must skip RHS")
    print("PASS: test_or_skips_on_success")


def test_semicolon_runs_both() -> None:
    """`echo a; echo b` runs both segments unconditionally."""
    with qemu_session(monitor=False, snapshot=True) as session:
        pre_length = len(session.buffer)
        session.send_command("echo first_a; echo second_b")
        between = output_after_command(session=session, pre_length=pre_length)
        assert_in(expected=b"first_a", haystack=between, label="; must run LHS")
        assert_in(expected=b"second_b", haystack=between, label="; must run RHS")
    print("PASS: test_semicolon_runs_both")


def main() -> int:
    """Build the OS image and run all chain smoke tests."""
    subprocess.run(
        ["./make_os.sh"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    test_and_runs_on_success()
    test_and_skips_on_failure()
    test_dollar_question_between_segments()
    test_mixed_chain_left_associative()
    test_or_runs_on_failure()
    test_or_skips_on_success()
    test_semicolon_runs_both()
    print("7 passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
