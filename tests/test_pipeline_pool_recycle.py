#!/usr/bin/env python3
"""Pipe pool recycles correctly across failure + success modes.

MAX_PIPES is 4.  Each pipeline allocates one slot in sys_pipeline2's
prologue.  Several unwind paths must each release that slot:

* ``.pipeline_b_not_found`` / ``.pipeline_b_not_execute`` /
  ``.pipeline_b_oom_handoff`` call ``pipe_release_by_index`` directly.
* ``.pipeline_c_not_found`` / siblings route through
  ``.pipeline_unwind_slot_b`` which releases the pipe.
* The normal-completion epilogue closes both children's pipe fds
  via ``child_terminate``'s fd_close loop; the last fd_close drives
  ``fd_close_pipe`` -> ``pipe_release``.
* ``spawn_failed_unwind`` (reached from
  ``build_child_program_state.oom`` for either child) walks the
  half-built child's fd_table with the same fd_close loop, so the
  ``FD_TYPE_PIPE_W`` / ``FD_TYPE_PIPE_R`` end installed before the
  build OOM is released the same way.

If any of those paths regresses to "leak the pool slot," running more
than MAX_PIPES pipelines through it eventually exhausts the pool and
``pipe_alloc`` returns -1 (surfaces as ``shell: pipeline failed``).
This test mixes the user-reachable failure modes with successes and
asserts the 16th run still succeeds — well past the 4-slot pool size.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from run_qemu import qemu_session  # noqa: E402


def _send(session: object, command: str, *, timeout: float = 8.0) -> bytes:
    pre = len(session.buffer)
    session.write_serial(command + "\r")
    with contextlib.suppress(TimeoutError):
        session.wait_for_substring(b"$ ", start=pre, timeout=timeout)
    return bytes(session.buffer[pre:])


def test_pipe_pool_recycles_across_failure_modes() -> None:
    """Mixed failure + success pipelines keep recycling the 4-slot pool."""
    failure_cmds = [
        "nope | pipe_consumer",  # .pipeline_b_not_found
        "pipe_producer | nope",  # .pipeline_c_not_found
    ]
    with qemu_session(monitor=False, snapshot=True, boot_timeout=10.0) as session:
        # Several rounds of failures interleaved with successes — more
        # than 4 of each so pool exhaustion would surface as a failing
        # success run.
        for _ in range(4):
            for cmd in failure_cmds:
                _send(session, cmd)
            success_output = _send(session, "pipe_producer | pipe_consumer")
            assert b"20" in success_output, f"pool recycle broke after failure mix; got {success_output!r}"
        # Final stretch: 6 back-to-back successes (above MAX_PIPES=4).
        for _ in range(6):
            output = _send(session, "pipe_producer | pipe_consumer")
            assert b"20" in output, f"pool exhausted on success path; got {output!r}"
    print("PASS: test_pipe_pool_recycles_across_failure_modes")


if __name__ == "__main__":
    test_pipe_pool_recycles_across_failure_modes()
