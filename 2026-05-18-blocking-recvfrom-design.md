# Blocking `recvfrom` with timeout

Status: design approved 2026-05-18. Implementation pending.

## Background

`SYS_NET_RECVFROM` (22h) is non-blocking today: returns `AX=0`
immediately if no packet matches the caller's local port. The NE2000
driver is polled (no IRQ), so packets only ingest when a userspace
caller explicitly drains the RX ring via `recvfrom`.

PR #370 fixed a `dns` flake by switching the userspace receive loop
from a 30,000-iteration busy poll to `sleep(1)` per iteration with a
5-second budget. `ping` carries a similar `sleep(1000)` between
receive checks. Both are workarounds for the lack of a blocking
primitive.

A `project-blocking-net-recvfrom` memory captured the followup but
framed the fix in Linux terms — kernel-side wait queues, parking
processes on a UDP socket, waking on NIC interrupt or deadline.
That framing is wrong for BBoeOS' current architecture (see below).

## Why a wait-queue / scheduler approach is wrong here

The pipe block-and-wake machinery (`kernel_yield_read` parks the
caller on a `struct pipe*`, scheduler runs the *other* pipeline slot,
`pipe_wake_reader` flips state back to `STATE_RUNNING`) only works
because pipelines have two concurrent userland slots — one parks, the
other runs and eventually writes, waking the parker.

For `recvfrom` there is no other userland slot to run. `dns` and
`ping` execute as the foreground program in `slot_a`. Parking that
slot leaves nothing to drain the NIC. Generalising the scheduler to
introduce a "kernel idle that polls the NIC" runnable is more
invasive than this followup warrants.

## Approach: kernel-side polled wait with `hlt`

The PIT already runs at 1 kHz (`PIT_DIVISOR = 1193` → ~999.85 Hz);
`system_ticks` increments every ~1 ms inside `pmode_irq0_handler`.

The new `sys_net_recvfrom` flow:

```
sys_net_recvfrom(fd, buf, max, local_port, timeout_ms):
    deadline = system_ticks + timeout_ms
    loop:
        try udp_receive / icmp_receive
        if matched -> copy into user buffer, return AX = bytes
        if timeout_ms == 0 -> return AX = 0   # preserves today's behaviour
        if system_ticks >= deadline -> return AX = 0
        sti; hlt                              # wake on next IRQ (≤1 ms)
```

`hlt` keeps CPU usage near-zero — the CPU sleeps until IRQ 0 (or any
other IRQ) fires. We resume, drain the NIC again, and either return a
packet or fall back to `hlt`. The NIC stays polled; we just poll it
kernel-side at PIT rate instead of userspace-side via a `sleep(1)` +
syscall round-trip per iteration.

This sidesteps wait queues entirely. No scheduler change. The pipe
block-and-wake remains the only client of `kernel_yield`.

## API change

`SYS_NET_RECVFROM` gains one register input:

- **ESI** = `timeout_ms` (unsigned). `0` preserves current
  non-blocking behaviour. `> 0` blocks up to that many milliseconds.

No "block forever" mode — every wait is bounded by an explicit
timeout. AX return is unchanged: bytes received, or `0` on timeout /
no match.

C signature:

```c
int recvfrom(int fd, void *buffer, int max_bytes,
             int local_port, int timeout_ms);
```

This is a breaking change to the 4-arg `recvfrom` signature. Only
two callers exist (`src/c/dns.c`, `src/c/ping.c`); both are updated
in the same PR. The `bboeos.h` header used by
`test_cc_compatibility` is updated to match (per the
`feedback-cc-compat-needs-header-decl` memory).

## Call-site simplifications

- `src/c/dns.c` — drop the `sleep(1)` polling loop; single
  `recvfrom(fd, buf, 512, 1024, 5000)` per query attempt.
- `src/c/ping.c` — drop the `sleep(1000)` between echoes; one
  `recvfrom(fd, buf, 128, 0, 1000)` per probe.

The 5-second total budget in `dns.c` and the 1-second per-probe
budget in `ping.c` collapse from outer loop bookkeeping into the
single timeout argument.

## Testing

- `tests/test_programs.py` already exercises `dns` and `ping` against
  the QEMU user-mode NAT — both must continue to pass.
- Add a smoke test that the non-blocking path (`timeout_ms = 0`)
  still returns `AX=0` immediately when no packet matches. This
  guards the carry-clear non-blocking contract that pre-existing
  callers depend on.
- The existing `dns` test exercises both the fast path (response
  arrives before the first `hlt`) and the slow path (response
  arrives after several ticks). No new test fixture required.

## Out of scope

- `select` / `poll` / `epoll`. The whole VFS lacks a readiness
  primitive; building one belongs to a later spec.
- A general scheduler wait queue. Pipe block-and-wake stays the only
  consumer of `kernel_yield`.
- Signal interruption of the wait (no `EINTR` story today; the wait
  terminates only on packet match or deadline).
- IRQ-driven NE2000 RX. Independent improvement; if/when it lands,
  the `hlt`/poll loop here naturally benefits (packet arrival
  becomes the wake source instead of just the PIT tick) but the
  syscall API does not change.

## Documentation updates

- `docs/syscalls.md` — note the new ESI input on `SYS_NET_RECVFROM`.
- `docs/CHANGELOG.md` — entry under Unreleased.
