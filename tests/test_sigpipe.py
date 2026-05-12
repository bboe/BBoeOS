#!/usr/bin/env python3
r"""SIGPIPE behaviour: default kill and SIG_IGN passthrough.

`pipe_drain` reads one byte then exits 9, closing the read end.  The
producer (`pipe_spam` with no args, or `pipe_spam ignore` for the
SIG_IGN passthrough variant) writes 16 KB into the 4 KB ring, so it
blocks once the buffer fills.  When the scheduler resumes it after the
consumer exits, `pipe_reader_open(p) == 0` and `fd_write_pipe` raises
SIGPIPE.

Default kill: kernel prints `^P\\n` via `signal_dispatch_kill` before
`child_terminate`.

SIG_IGN: `pipe_spam ignore` installs SIG_IGN before writing, so the
syscall epilogue clears pending_sigpipe and lets the -1 return surface;
the program returns 5.  We can't observe the writer's exit status (the
shell `cmd1 | cmd2` plumbing returns the right-hand child's status),
so we just verify the absence of `^P` and that the shell prompt
returns.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def _run(*, command: str, timeout: float = 15.0) -> bytes:
    with qemu_session(monitor=False, snapshot=True, boot_timeout=10.0) as session:
        pre = len(session.buffer)
        session.write_serial(command + "\r")
        with contextlib.suppress(TimeoutError):
            session.wait_for_substring(b"$ ", start=pre, timeout=timeout)
        return bytes(session.buffer[pre:])


def test_sigpipe_default_kills_producer() -> None:
    """SIG_DFL on broken pipe terminates the writer and prints ^P."""
    out = _run(command="pipe_spam | pipe_drain")
    assert b"^P" in out, f"expected '^P' from signal_dispatch_kill; got {out!r}"
    print("PASS: test_sigpipe_default_kills_producer")


def test_sigpipe_ignored_returns_epipe() -> None:
    """SIG_IGN on broken pipe lets write() return -1; no kernel kill."""
    out = _run(command="pipe_spam ignore | pipe_drain")
    assert b"^P" not in out, f"unexpected '^P' (SIG_IGN should suppress kill); got {out!r}"
    assert b"$ " in out, f"shell did not return to prompt; got {out!r}"
    print("PASS: test_sigpipe_ignored_returns_epipe")


if __name__ == "__main__":
    test_sigpipe_default_kills_producer()
    test_sigpipe_ignored_returns_epipe()
