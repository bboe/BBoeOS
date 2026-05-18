# Blocking `recvfrom` via `SO_RCVTIMEO`

Status: design approved 2026-05-18 (supersedes the earlier same-day
design that put `timeout_ms` on the `recvfrom` argument list — see
"History" below). Implementation pending.

## Background

`SYS_NET_RECVFROM` (22h) is non-blocking today: returns `AX=0`
immediately if no packet matches the caller's local port. The NE2000
driver is polled (no IRQ), so packets only ingest when a userspace
caller explicitly drains the RX ring via `recvfrom`.

PR #370 fixed a `dns` flake by switching the userspace receive loop
from a 30,000-iteration busy poll to `sleep(1)` per iteration with a
5-second budget. `ping` carried a similar `sleep(1000)` between
receive checks. Both were workarounds for the lack of a blocking
primitive.

## Design

Two pieces:

1. **A new `SYS_NET_SETSOCKOPT` syscall (24h)** that stores
   per-socket options on the fd entry. The only option this PR
   implements is `SO_RCVTIMEO` (receive timeout in milliseconds). The
   syscall is structured so additional options (`SO_SNDTIMEO`,
   `SO_BROADCAST`, …) can land later without further syscall churn.

2. **`sys_net_recvfrom` reads the timeout from the socket's fd
   entry**, not from its register inputs. The existing 4-arg
   `recvfrom` signature is preserved. When the stored timeout is `0`,
   behaviour is byte-identical to today's non-blocking call. When
   it's `>0`, the kernel loops on `sti; hlt; cli`, draining the NIC
   each PIT tick, until a matching packet arrives or `system_ticks`
   reaches the deadline.

### Why not put `timeout_ms` on `recvfrom`?

POSIX puts timeouts on the socket (`setsockopt(SO_RCVTIMEO)`), not on
the recv call. Adding a `timeout_ms` arg to `recvfrom` conflates
"wait policy" (a socket attribute) with "do the operation" (the
recv), forces every callsite to think about timeout even when the
natural answer is "use the socket default", and doesn't compose if a
future `read()` ever wants the same treatment. The socket-options
shape extends cleanly to additional options later.

### `SYS_NET_SETSOCKOPT` register convention

- `BX` = fd
- `AL` = option name (one byte; `SO_RCVTIMEO = 1` for this PR)
- `ECX` = value (32-bit; for `SO_RCVTIMEO`, milliseconds — `0`
  disables blocking, `>0` is the deadline budget per `recvfrom` call)
- Returns `CF` set on error (bad fd, unknown option, value out of
  range); `CF` clear on success. No `AX` output.

C signature in `tests/bboeos.h`:

```c
int setsockopt(int fd, int option_name, int value);
```

Returns `0` on success, `-1` on error (so callers can write the
familiar `if (setsockopt(...) < 0) { ... }` shape).

### Storage on the fd entry

`struct fd` (`src/fs/fd.c`, 64 bytes total with a 12-byte `_rest`
tail) gains a `uint32_t recv_timeout_ms` field consuming 4 bytes of
the existing tail padding. `fd_close` already `memset`s the whole
entry on close, so the field naturally resets to `0` (non-blocking
default) on fd reuse.

A matching `FD_OFFSET_RECV_TIMEOUT_MS` constant lands in
`src/include/constants.asm` for asm callers, alongside the existing
`FD_OFFSET_*` cluster.

### Kernel-side polled wait — unchanged from prior design

The PIT runs at 1 kHz (`PIT_DIVISOR = 1193` → ~999.85 Hz);
`system_ticks` increments every ms in `pmode_irq0_handler`.
`sys_net_recvfrom`'s loop:

```
sys_net_recvfrom(fd, buf, max, local_port):
    look up fd entry; bail on bad fd / wrong type
    timeout_ms = entry->recv_timeout_ms
    loop:
        try udp_receive / icmp_receive (UDP filters by local_port)
        if matched -> copy, return AX = bytes
        if timeout_ms == 0 -> return AX = 0   # preserves today's behaviour
        # Lazily compute deadline on first miss:
        if !have_deadline: deadline = system_ticks + timeout_ms
        if system_ticks >= deadline -> return AX = 0
        # Only sleep when no packet was drained this iteration; a
        # non-matching UDP packet bumps us back into a re-poll without
        # `hlt`, but still respects the wall-clock deadline.
        if !had_packet: sti; hlt; cli
```

`hlt` keeps CPU usage near-zero — the CPU sleeps until IRQ 0 (or
another IRQ) fires. Non-matching UDP packets do not extend the
deadline (the cap is wall-clock, not packets-seen).

### Why the kernel polls instead of parking on a wait queue

The pipe block-and-wake machinery (`kernel_yield_read` parks the
caller on a `struct pipe*`, scheduler runs the *other* pipeline
slot, `pipe_wake_reader` flips state back to `STATE_RUNNING`) only
works because pipelines have two concurrent userland slots — one
parks, the other runs and eventually writes, waking the parker.

For `recvfrom` there is no other userland slot to run. `dns` and
`ping` execute as the foreground program in `slot_a`. Parking that
slot leaves nothing to drain the NIC. Generalising the scheduler to
introduce a "kernel idle that polls the NIC" runnable is more
invasive than this followup warrants.

## API summary

```c
// Returns 0 on success, -1 on error (bad fd, unknown option).
int setsockopt(int fd, int option_name, int value);

// Returns bytes received in AX, or 0 if no matching packet (and the
// per-fd SO_RCVTIMEO deadline elapsed, if set).  CF clear on the
// normal path.  Signature unchanged from today.
int recvfrom(int fd, void *buffer, int max_bytes, int local_port);

// Option names for setsockopt:
#define SO_RCVTIMEO 1
```

Default `recv_timeout_ms` is `0` (non-blocking, matching today's
behaviour), so this is a strictly additive change — every existing
4-arg `recvfrom` caller keeps working unchanged.

## Call-site updates

- `src/c/dns.c` — `setsockopt(socket_fd, SO_RCVTIMEO, 5000)` after
  the socket opens, then a single `recvfrom(socket_fd, buf, 512,
  1024)` per query attempt. The old `sleep(1)` polling loop drops.
- `src/c/ping.c` — `setsockopt(fd, SO_RCVTIMEO, 1000)` after socket
  open (echo path); per-probe `recvfrom(fd, ...)` blocks up to the 1
  sec budget. The `resolve_dns()`-style non-blocking probe at the top
  of the file doesn't call `setsockopt` — it inherits the default of
  `0` (non-blocking), matching its pre-existing semantics.

## Archive `.asm` parity

`src/include/dns_query.asm` (used by `archive/dns.asm`) and
`archive/ping.asm` are updated in the same commit as their `.c`
counterparts — per the project's `feedback-archive-byte-parity`
memory:

- Both `.asm` files emit a `SYS_NET_SETSOCKOPT` syscall right after
  `SYS_NET_OPEN` (loading `BX`=fd, `AL`=`SO_RCVTIMEO`, `ECX`=ms).
- Both `.asm` files drop their `.poll` loops and the polling-budget
  decrement around `SYS_NET_RECVFROM`.

## Testing

- `tests/test_programs.py` already exercises `dns` and `ping` — both
  must continue to pass.
- `tests/programs/recv_nonblock_test.c` (new): opens a UDP socket,
  does **not** call `setsockopt`, calls `recvfrom`, verifies it
  returns `AX=0` immediately on an empty queue (guards the default-0
  contract).
- `tests/programs/recv_timeout_test.c` (new): opens a UDP socket,
  calls `setsockopt(fd, SO_RCVTIMEO, 200)`, calls `recvfrom`, checks
  that elapsed `uptime()` ticks past the second boundary (the call
  blocked) and AX=0 (timeout fired).
- Full `.github/workflows/test.yml` matrix locally.

## Out of scope

- Any `getsockopt`. We only need to set in this PR.
- `SO_SNDTIMEO`, `SO_BROADCAST`, `SO_REUSEADDR`, etc. — the syscall
  surface and the `option_name` byte make these cheap to add later.
- `select` / `poll` / `epoll`.
- Signal interruption (no `EINTR` today; the wait terminates only on
  packet match or deadline).
- IRQ-driven NE2000 RX.

## Documentation updates

- `docs/syscalls.md` — add `SYS_NET_SETSOCKOPT` (24h) row.
- `docs/CHANGELOG.md` — entry under Unreleased.

## History

A first design (committed earlier today, see git history of this
file) put `timeout_ms` directly on the `recvfrom` argument list (ESI
input). It worked and shipped to PR #411, but was closed pre-merge:
adding a per-call timeout deviates from POSIX, conflates two
concerns, and doesn't generalise to other per-socket options. The
present design moves the timeout onto a proper socket attribute set
via `SYS_NET_SETSOCKOPT`, preserves the 4-arg `recvfrom` signature,
and keeps every other piece of the kernel-side polled-wait machinery
intact.
