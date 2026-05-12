#!/usr/bin/env python3
"""Cooperative pipes basic smoke via shell `|` syntax.

Exercises SYS_SYS_PIPELINE2 end-to-end: shell parses `pipe_producer |
pipe_consumer`, calls pipeline2(bin/pipe_producer, bin/pipe_consumer),
the kernel runs both children cooperatively, the consumer reads the
producer's 20 bytes + EOF and prints the byte count.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def _run(*, command: str, timeout: float = 15.0) -> bytes:
    # Extend the boot timeout so we can reliably capture the first prompt.
    with qemu_session(monitor=False, snapshot=True, boot_timeout=10.0) as session:
        pre = len(session.buffer)
        # Use write_serial instead of send_command (which has a short
        # COMMAND_TIMEOUT that would fire before the pipeline finishes).
        session.write_serial(command + "\r")
        # wait_for_substring actively drains the serial FIFO.
        with contextlib.suppress(TimeoutError):
            session.wait_for_substring(b"$ ", start=pre, timeout=timeout)
        return bytes(session.buffer[pre:])


def test_pipeline_basic() -> None:
    """`pipe_producer | pipe_consumer` should print '20' (byte count)."""
    out = _run(command="pipe_producer | pipe_consumer")
    assert b"20" in out, f"expected '20' in output; got {out!r}"
    print("PASS: test_pipeline_basic")


if __name__ == "__main__":
    test_pipeline_basic()
