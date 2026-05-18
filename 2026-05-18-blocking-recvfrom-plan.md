# Blocking `recvfrom` via `SO_RCVTIMEO` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `SYS_NET_SETSOCKOPT(fd, option_name, value)` syscall whose only currently-supported option is `SO_RCVTIMEO`. Store the receive timeout on the socket's `struct fd` entry. `sys_net_recvfrom`'s 4-arg signature is preserved; the kernel reads `entry->recv_timeout_ms` and, if non-zero, loops on `sti; hlt; cli` (draining the NIC each PIT tick) until a matching packet arrives or `system_ticks >= deadline`. `dns` and `ping` (both `.c` and archive `.asm`) call `setsockopt` once after `socket()` and stop doing userspace `sleep(...)` polling.

**Architecture:** Socket-options on the fd entry; kernel-side polled wait; default `recv_timeout_ms = 0` preserves today's non-blocking contract.

**Tech Stack:** cc.py (BBoeOS-flavour C with inline asm), NASM, QEMU integration tests under `tests/`.

**Spec:** [`2026-05-18-blocking-recvfrom-design.md`](./2026-05-18-blocking-recvfrom-design.md)

**Supersedes:** the same-date PR #411 design that put `timeout_ms` on the `recvfrom` argument list. That PR was closed pre-merge; this branch starts fresh from `main`.

---

## Setup

### Task 0: Branch off main

**Files:** none (git only)

- [ ] **Step 1: Create the implementation branch**

```bash
cd /home/ubuntu/bboeos/.claude/worktrees/other
git fetch origin main
git checkout -B bboe/recv-timeout-setsockopt origin/main
git log --oneline -1
```

Expected: HEAD points at the latest `main` commit.

---

## Phase 1 — `setsockopt` plumbing

### Task 1: Add the `SO_RCVTIMEO` / `SYS_NET_SETSOCKOPT` constants and the FD offset

**Files:**
- Modify: `src/include/constants.asm`

- [ ] **Step 1: Add the new syscall number**

In `src/include/constants.asm`, find the `SYS_NET_*` cluster (around lines 206–209). Add, keeping alphabetical / numeric order:

```asm
%assign SYS_NET_SETSOCKOPT 24h
```

- [ ] **Step 2: Add the option-name constant**

Add `SO_RCVTIMEO` somewhere appropriate in the constants file (group with other socket-level constants like `SOCK_DGRAM`, `IPPROTO_UDP`):

```asm
%assign SO_RCVTIMEO 1
```

If a `SOCK_*` / `IPPROTO_*` block exists, place `SO_*` adjacent to it; otherwise add a new mini-block.

- [ ] **Step 3: Add the FD offset for `recv_timeout_ms`**

In the `FD_OFFSET_*` cluster (around lines 33–44), add (offset 52 — the start of the existing `_rest[12]` padding):

```asm
%assign FD_OFFSET_RECV_TIMEOUT_MS 52   ; uint32 ms; 0 = non-blocking
```

Keep the cluster alphabetically sorted.

- [ ] **Step 4: Commit**

```bash
git add src/include/constants.asm
git commit -m "constants: add SYS_NET_SETSOCKOPT, SO_RCVTIMEO, recv-timeout fd offset"
```

### Task 2: Add `recv_timeout_ms` to `struct fd`

**Files:**
- Modify: `src/fs/fd.c` (the `struct fd` declaration around line 32)

- [ ] **Step 1: Add the field**

In `struct fd` add `uint32_t recv_timeout_ms;` at offset 52 (the start of the existing `_rest[12]` tail). Shrink `_rest` to 8 bytes to keep the total at 64 bytes.

```c
struct fd {
    uint8_t type;
    uint8_t flags;
    uint16_t start;
    int size;
    int position;
    uint16_t directory_sector;
    uint16_t directory_offset;
    uint8_t mode;
    uint8_t event_head;
    uint8_t event_tail;
    uint8_t dirty;
    uint8_t event_buf[32];
    uint32_t recv_timeout_ms;
    uint8_t _rest[8];
};
```

Verify the offset matches `FD_OFFSET_RECV_TIMEOUT_MS = 52`:
1 (type) + 1 (flags) + 2 (start) + 4 (size) + 4 (position) + 2 (dir_sec) + 2 (dir_off) + 1 (mode) + 1 (event_head) + 1 (event_tail) + 1 (dirty) + 32 (event_buf) = 52. Check.

- [ ] **Step 2: Confirm the struct still totals 64**

Run:

```bash
python3 cc.py --bits 32 src/fs/fd.c /tmp/fd.asm 2>&1 | tail
```

(Or just visually count.) `FD_ENTRY_SIZE = 64` is asserted at runtime/build by other code paths; if the struct shrunk, things break.

- [ ] **Step 3: Commit**

```bash
git add src/fs/fd.c
git commit -m "fd: add recv_timeout_ms field for SO_RCVTIMEO"
```

### Task 3: Implement `sys_net_setsockopt`

**Files:**
- Modify: `src/syscall/syscalls.c` (add the new syscall handler)
- Modify: `src/arch/x86/syscall.asm` (add the dispatch entry)

- [ ] **Step 1: Add `sys_net_setsockopt` to `src/syscall/syscalls.c`**

Add the new handler in alphabetical position with the other `sys_net_*` handlers. (Today the file has `sys_net_open`, `sys_net_recvfrom`, `sys_net_sendto`. `sys_net_setsockopt` sorts after `sys_net_sendto`.)

```c
// sys_net_setsockopt: store a per-socket option on the fd entry.
// BX = fd, AL = option_name (SO_RCVTIMEO), ECX = value.
// Returns 0 on success (CF clear), -1 on error (CF set):
//   - bad fd
//   - fd is not a socket (not UDP/ICMP/NET)
//   - unknown option_name
//   - SO_RCVTIMEO value > INT32_MAX (defensive; treat negative as error)
__attribute__((carry_return)) int
sys_net_setsockopt(int *result __attribute__((out_register("ax"))),
                   int fd_num __attribute__((in_register("bx"))),
                   int option_name __attribute__((in_register("ax"))),
                   int value __attribute__((in_register("ecx")))) {
    struct fd *entry;
    if (!fd_lookup(fd_num, &entry)) {
        *result = -1;
        return 0;
    }
    if (entry->type != FD_TYPE_UDP && entry->type != FD_TYPE_ICMP
        && entry->type != FD_TYPE_NET) {
        *result = -1;
        return 0;
    }
    if ((option_name & 0xFF) == SO_RCVTIMEO) {
        if (value < 0) {
            *result = -1;
            return 0;
        }
        entry->recv_timeout_ms = (uint32_t)value;
        *result = 0;
        return 1;
    }
    *result = -1;
    return 0;
}
```

(Note: AL contains both the syscall number setup *and* the option name. The dispatcher's prelude must save `option_name` from AL before `AH` is reused. Step 2 handles this.)

- [ ] **Step 2: Add the dispatcher entry in `src/arch/x86/syscall.asm`**

Find the `.net_recvfrom` / `.net_sendto` dispatch entries (around lines 406–425) and add `.net_setsockopt` after them, alphabetically:

```asm
        .net_setsockopt:
        ;; BX = fd, AL = option_name, ECX = value.
        call sys_net_setsockopt
        jmp .iret_cf
```

Then in the SYS_ENTRY table (around line 120) add:

```asm
SYS_ENTRY SYS_NET_SETSOCKOPT, .net_setsockopt
```

(Insert alphabetically with the existing `SYS_NET_*` entries.)

The dispatcher's prelude already preserves `EAX` low byte (`AL`) into the right slot for register-attribute pickup; the `int option_name __attribute__((in_register("ax")))` in `sys_net_setsockopt` reads from the saved-AX register. Look at how `sys_io_seek` (which uses `AL` for `whence`) does this — copy that pattern exactly.

- [ ] **Step 3: Build**

```bash
./make_os.sh 2>&1 | tail -10
```

Expected: clean build. The new syscall isn't called yet, but the dispatcher should still resolve and link.

- [ ] **Step 4: Commit**

```bash
git add src/syscall/syscalls.c src/arch/x86/syscall.asm
git commit -m "syscall: add SYS_NET_SETSOCKOPT for SO_RCVTIMEO"
```

### Task 4: Add `setsockopt` to cc.py and `tests/bboeos.h`

**Files:**
- Modify: `cc/codegen/x86/builtins.py` (new builtin method `builtin_setsockopt`)
- Modify: `cc/codegen/x86/generator.py` (pinned register set entry)
- Modify: `cc/target.py` (syscall name → opcode mapping)
- Modify: `tests/bboeos.h` (prototype)

- [ ] **Step 1: Add the syscall opcode mapping**

In `cc/target.py`, find the dict mapping syscall short-names to their `mov ah, SYS_XXX / int 30h` emission (around line 202). Add (alphabetical with other `NET_*` entries):

```python
        "NET_SETSOCKOPT": ("mov ah, SYS_NET_SETSOCKOPT", "int 30h"),
```

- [ ] **Step 2: Add the pinned register set**

In `cc/codegen/x86/generator.py`, find the dict keyed on builtin name → frozenset (around line 130; where the `recvfrom` entry lives). Add:

```python
        "setsockopt": frozenset({"ax", "bx", "cx"}),
```

(AX = both syscall number byte and `option_name`; BX = fd; ECX = value. No DX, DI, SI, BP used.)

- [ ] **Step 3: Add the builtin method**

In `cc/codegen/x86/builtins.py`, add (alphabetically, between `builtin_send` / `builtin_socket` or similar — check existing order):

```python
    def builtin_setsockopt(self, arguments: list[Node], /) -> None:
        """Generate code for the setsockopt() builtin.

        ``setsockopt(fd, option_name, value)`` emits register setup
        followed by ``mov ah, SYS_NET_SETSOCKOPT / int 30h``.
        Returns 0 in AX on success, -1 on error (bad fd, wrong fd
        type, unknown option, negative value).  Argument loads are
        topologically scheduled by
        :meth:`_emit_builtin_arg_moves`.
        """
        self._check_argument_count(arguments=arguments, expected=3, name="setsockopt")
        fd_argument, option_argument, value_argument = arguments
        self._emit_builtin_arg_moves([
            (self.target.bx_register, fd_argument),
            (self.target.ax_register, option_argument),
            (self.target.count_register, value_argument),
        ])
        self._emit_syscall("NET_SETSOCKOPT")
        self.ax_clear()
```

- [ ] **Step 4: Register the builtin in the dispatcher**

cc.py has a mapping from C identifier → builtin method somewhere. Grep for how `recvfrom` is wired in (e.g. `grep -rn "builtin_recvfrom\|'recvfrom'" cc/`). Add an analogous entry for `setsockopt`.

- [ ] **Step 5: Add the prototype to `tests/bboeos.h`**

Alphabetically with the other declarations:

```c
/* setsockopt — set a per-socket option.  Currently supports
   SO_RCVTIMEO (option_name=1, value=ms; 0 disables blocking).
   Returns 0 on success, -1 on bad fd / unknown option / negative
   value. */
int setsockopt(int fd, int option_name, int value);
```

Add a `#define SO_RCVTIMEO 1` near the top of `bboeos.h` if other `#define`d constants live there; otherwise document the value `1` in the prototype comment.

- [ ] **Step 6: Run cc.py unit tests**

```bash
python -m pytest tests/unit/ -x -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add cc/codegen/x86/builtins.py cc/codegen/x86/generator.py cc/target.py tests/bboeos.h
git commit -m "cc: add setsockopt(fd, option, value) builtin"
```

---

## Phase 2 — Kernel: read timeout from fd

### Task 5: Update `sys_net_recvfrom` to read timeout from the fd

**Files:**
- Modify: `src/syscall/syscalls.c` (the `sys_net_recvfrom` function)

The 4-arg signature is preserved. The function reads `entry->recv_timeout_ms` and uses it exactly like the prior `timeout_ms` argument did.

- [ ] **Step 1: Replace `sys_net_recvfrom`**

```c
__attribute__((carry_return)) int
sys_net_recvfrom(int *bytes_copied __attribute__((out_register("ax"))),
                 int fd_num __attribute__((in_register("bx"))),
                 uint8_t *user_buffer __attribute__((in_register("edi"))),
                 int max_bytes __attribute__((in_register("ecx"))),
                 int local_port __attribute__((in_register("dx")))) {
    struct fd *entry;
    uint8_t *payload;
    int payload_length;
    int dest_port;
    uint32_t timeout_ms;
    uint32_t deadline;
    int have_deadline;
    int had_packet;
    if (!fd_lookup(fd_num, &entry)) {
        *bytes_copied = 0;
        return 1;
    }
    if (entry->type != FD_TYPE_UDP && entry->type != FD_TYPE_ICMP) {
        *bytes_copied = 0;
        return 1;
    }
    timeout_ms = entry->recv_timeout_ms;
    have_deadline = 0;
    deadline = 0;
    while (1) {
        had_packet = 0;
        if (entry->type == FD_TYPE_UDP) {
            if (udp_receive(&payload, &payload_length)) {
                had_packet = 1;
                dest_port = (net_receive_buffer[36] << 8)
                            | net_receive_buffer[37];
                if (dest_port == (local_port & 0xFFFF)) {
                    if (payload_length > max_bytes) {
                        payload_length = max_bytes;
                    }
                    memcpy(user_buffer, payload, payload_length);
                    *bytes_copied = payload_length;
                    return 1;
                }
                // Non-matching UDP packet: still counts against the
                // wall-clock deadline (fall through to deadline check).
            }
        } else {
            if (icmp_receive(&payload, &payload_length)) {
                had_packet = 1;
                if (payload_length > max_bytes) {
                    payload_length = max_bytes;
                }
                memcpy(user_buffer, payload, payload_length);
                *bytes_copied = payload_length;
                return 1;
            }
        }
        if (timeout_ms == 0) {
            *bytes_copied = 0;
            return 1;
        }
        if (!have_deadline) {
            deadline = rtc_tick_read() + timeout_ms;
            have_deadline = 1;
        }
        if (rtc_tick_read() >= deadline) {
            *bytes_copied = 0;
            return 1;
        }
        if (!had_packet) {
            kernel_hlt_idle();
        }
    }
}
```

This is the kernel logic from the prior (closed) PR with two surface changes:
- Signature drops the ESI input; `timeout_ms` now comes from `entry->recv_timeout_ms`.
- Type is `uint32_t` directly (no signed cast — the syscall already rejects negative `SO_RCVTIMEO` values).

The supporting helpers (`kernel_hlt_idle` inline asm, `rtc_tick_read` forward decl) must also exist in this file — copy them from the prior PR's commit `05f50540`:

```c
uint32_t rtc_tick_read();  // returns EAX = system_ticks atomically; preserves all other regs

// kernel_hlt_idle — enable interrupts, halt until the next IRQ, then
// disable interrupts again.  Used by sys_net_recvfrom's wait loop so
// the CPU sleeps between PIT ticks instead of busy-spinning.  The
// surrounding syscall handler entered with IF=0 (interrupt gate), so
// we restore that state on return.
void kernel_hlt_idle();
asm("kernel_hlt_idle:\n"
    "    sti\n"
    "    hlt\n"
    "    cli\n"
    "    ret\n");
```

Place the forward decl alphabetically with the other forward decls; place the `kernel_hlt_idle` helper right above `sys_net_recvfrom`.

Update the comment block above `sys_net_recvfrom` so it accurately describes reading the timeout from the fd entry.

- [ ] **Step 2: Build**

```bash
./make_os.sh 2>&1 | tail -10
```

Expected: clean build.

- [ ] **Step 3: Commit**

```bash
git add src/syscall/syscalls.c
git commit -m "syscall: block in sys_net_recvfrom per fd SO_RCVTIMEO"
```

---

## Phase 3 — Userspace callsites

### Task 6: dns.c uses `setsockopt(SO_RCVTIMEO, 5000)`

**File:** `src/c/dns.c`

- [ ] **Step 1: Read the current dns.c receive area**

Find the section after `socket(...)` returns the fd and before the recvfrom poll loop.

- [ ] **Step 2: Add `setsockopt` after socket open**

Right after the socket is opened (and the success check passes), call:

```c
setsockopt(socket_fd, SO_RCVTIMEO, 5000);
```

(No error check — if the call fails for any reason, recvfrom falls back to non-blocking, which is no worse than today's behaviour. dns will just timeout faster than it should. A clean `if (setsockopt(...) < 0) die("setsockopt\n");` is acceptable if you want one — but the dns flow has no clean recovery either way.)

- [ ] **Step 3: Replace the polling loop with a single blocking recvfrom**

Replace the loop with:

```c
received = recvfrom(socket_fd, query_buffer, 512, 1024);
close(socket_fd);
if (received == 0) {
    die("dns: no response\n");
}
```

Drop the iteration counter and the trailing `sleep(1)`. Match the existing error-message style (the prior PR landed `"dns: no response\n"` — that's fine to reuse).

- [ ] **Step 4: Build and run the dns test**

```bash
./make_os.sh
python tests/test_programs.py dns 2>&1 | tail -10
```

ping.c will still compile fine because recvfrom is back to 4-arg.

- [ ] **Step 5: Commit**

```bash
git add src/c/dns.c
git commit -m "dns: use setsockopt(SO_RCVTIMEO, 5000) + blocking recvfrom"
```

### Task 7: ping.c uses `setsockopt(SO_RCVTIMEO, 1000)`

**File:** `src/c/ping.c`

- [ ] **Step 1: Read ping.c around the echo socket open and the per-echo recvfrom**

Identify the socket-open + per-echo-recv area (the original Task 5 location, ~lines 130–165 in the prior PR).

- [ ] **Step 2: Add setsockopt + drop the outer `tries=30000` wrapper**

After the echo socket opens (the `fd = socket(...)` near line 130 area), insert:

```c
setsockopt(fd, SO_RCVTIMEO, 1000);
```

In the per-echo recvfrom loop, drop the outer `tries=30000` wrapper entirely (the bug we caught in the prior PR's final review). Replace the whole block with:

```c
int n = recvfrom(fd, packet_buffer, 128, 0);
int got = (n > 0 && packet_buffer[0] == '\0');
```

Then also delete any remaining `sleep(1000)` that previously paced the loop.

- [ ] **Step 3: The bring-up probe inside `resolve_dns()`**

The `resolve_dns()` helper at the top of ping.c also opens a UDP socket and calls `recvfrom` as a non-blocking probe. The default `recv_timeout_ms = 0` preserves that semantic, so **do not** add a `setsockopt` call there. The `recvfrom(fd, query, 512, 1024)` call stays 4-arg and stays non-blocking by default.

- [ ] **Step 4: Build and run both tests**

```bash
./make_os.sh
python tests/test_programs.py ping 2>&1 | tail -10
python tests/test_programs.py dns 2>&1 | tail -10
```

Both must pass.

- [ ] **Step 5: Commit**

```bash
git add src/c/ping.c
git commit -m "ping: use setsockopt(SO_RCVTIMEO, 1000) + blocking recvfrom"
```

---

## Phase 4 — Archive `.asm` parity

### Task 8: Update `src/include/dns_query.asm`

**File:** `src/include/dns_query.asm`

After the `SYS_NET_OPEN` block (which stores the fd in `dns_socket_fd`), insert a `SYS_NET_SETSOCKOPT` call:

```asm
        ;; setsockopt(fd, SO_RCVTIMEO, 5000) — 5 second blocking recv
        mov ebx, [dns_socket_fd]
        mov al, SO_RCVTIMEO
        mov ecx, 5000
        mov ah, SYS_NET_SETSOCKOPT
        int 30h
```

(No error check — same rationale as the C version.)

Then collapse the `.poll` loop around `SYS_NET_RECVFROM` to a single call, the same way the prior PR did:

```asm
        ;; Blocking recv (kernel uses fd's SO_RCVTIMEO of 5000 ms)
        mov ebx, [dns_socket_fd]
        mov edi, SECTOR_BUFFER
        mov ecx, 512
        mov dx, 1024
        mov ah, SYS_NET_RECVFROM
        int 30h
        test eax, eax
        jz .err_close
```

The `.got_response:` label that used to mark the success branch is now natural fall-through — delete or keep, your choice.

- [ ] **Step 1: Apply both edits**
- [ ] **Step 2: Build, run test_asm.py**

```bash
./make_os.sh
python tests/test_asm.py 2>&1 | tail -5
```

`test_asm.py` reassembles each archive via the self-hosted assembler and diffs against NASM. Both must pass.

- [ ] **Step 3: Commit (will batch with Task 9)**

Don't commit yet; combine with the archive/ping.asm fix and the README size-table update.

### Task 9: Update `archive/ping.asm`

**File:** `archive/ping.asm`

After the `SYS_NET_OPEN` for the echo socket, insert the equivalent `SYS_NET_SETSOCKOPT(SO_RCVTIMEO, 1000)` call. Then drop the `.poll`/`dec ebp / jnz .poll` machinery around the echo-reply recvfrom — single blocking call, same pattern as Task 8.

The bring-up / probe path (if archive/ping.asm has one) stays default-0 (no setsockopt call).

Also drop any inter-echo `SYS_RTC_SLEEP` / sleep loop, the same way the C version did.

- [ ] **Step 1: Apply edits**
- [ ] **Step 2: Verify test_asm.py still passes**

### Task 10: Update `archive/README.md` size table

**File:** `archive/README.md`

Run:

```bash
python tests/test_archive.py 2>&1 | tail -30
```

Read the "size drift" output for `dns` and `ping`. Update those rows in `archive/README.md` to match. The script will tell you exactly what numbers to use.

- [ ] **Step 1: Apply the table edits**
- [ ] **Step 2: Re-run test_archive.py — expect PASS**

```bash
python tests/test_archive.py 2>&1 | tail -10
```

- [ ] **Step 3: Commit (batched)**

```bash
git add archive/ping.asm src/include/dns_query.asm archive/README.md
git commit -m "archive: mirror SO_RCVTIMEO in dns/ping asm sources"
```

---

## Phase 5 — Regression coverage

### Task 11: Update `recv_nonblock_test` and add `recv_timeout_test`

**Files:**
- Create: `tests/programs/recv_nonblock_test.c` (the prior PR's version is good — it tests the default-0 path; bring it over)
- Create: `tests/programs/recv_timeout_test.c` (new — verifies SO_RCVTIMEO triggers blocking + timeout)
- Modify: `tests/test_programs.py` (two ProgramTest entries)

- [ ] **Step 1: Write `recv_nonblock_test.c`**

```c
/* recv_nonblock_test — verify recvfrom returns AX=0 immediately when
   no matching packet is queued and SO_RCVTIMEO was NOT set.  This
   guards the default-0 non-blocking contract.  Prints OK on success,
   FAIL: <reason> otherwise. */
int main() {
    int file_descriptor;
    char buffer[64];
    int received;
    int before;
    int after;
    file_descriptor = net_open(SOCK_DGRAM, IPPROTO_UDP);
    if (file_descriptor < 0) {
        printf("FAIL: socket\n");
        return 1;
    }
    before = uptime();
    received = recvfrom(file_descriptor, buffer, 64, 65000);
    after = uptime();
    close(file_descriptor);
    if (received != 0) {
        printf("FAIL: got bytes\n");
        return 1;
    }
    if (after != before) {
        printf("FAIL: blocked\n");
        return 1;
    }
    printf("OK\n");
    return 0;
}
```

(The exact builtin names follow the project conventions — `net_open`, not `socket`. Look at the prior PR's commit `9c07cc2c` for the exact shape.)

- [ ] **Step 2: Write `recv_timeout_test.c`**

```c
/* recv_timeout_test — verify SO_RCVTIMEO causes recvfrom to block
   and to return AX=0 on deadline.  Sets a 200 ms timeout, calls
   recvfrom on an empty queue, expects AX=0 and uptime() to have
   advanced at least one tick (i.e., the call DID block).  Prints OK
   on success, FAIL: <reason> otherwise. */
int main() {
    int file_descriptor;
    char buffer[64];
    int received;
    int before;
    int after;
    int set_result;
    file_descriptor = net_open(SOCK_DGRAM, IPPROTO_UDP);
    if (file_descriptor < 0) {
        printf("FAIL: socket\n");
        return 1;
    }
    set_result = setsockopt(file_descriptor, SO_RCVTIMEO, 200);
    if (set_result != 0) {
        printf("FAIL: setsockopt\n");
        return 1;
    }
    before = uptime();
    received = recvfrom(file_descriptor, buffer, 64, 65000);
    after = uptime();
    close(file_descriptor);
    if (received != 0) {
        printf("FAIL: got bytes\n");
        return 1;
    }
    /* uptime() is second-granularity; a 200 ms wait may or may not
       cross a second boundary.  Just confirm we didn't block much
       longer than the budget (e.g. 5+ seconds), which would indicate
       the timeout didn't fire. */
    if (after - before > 1) {
        printf("FAIL: blocked too long\n");
        return 1;
    }
    printf("OK\n");
    return 0;
}
```

If `uptime()` is only second-granularity and 200 ms is too short to be observable, swap for a 1500 ms timeout and assert `after - before == 1` or `after - before == 2`.

- [ ] **Step 3: Add both ProgramTest entries**

In `tests/test_programs.py`, alphabetically — `recv_nonblock_test` and `recv_timeout_test` both sort between `recursive_exec_test` and any later `r*` entry:

```python
    ProgramTest(
        "recv_nonblock_test",
        ["recv_nonblock_test"],
        r"^OK$",
        with_net=True,
    ),
    ProgramTest(
        "recv_timeout_test",
        ["recv_timeout_test"],
        r"^OK$",
        with_net=True,
    ),
```

- [ ] **Step 4: Build and run both**

```bash
./make_os.sh
python tests/test_programs.py recv_nonblock_test recv_timeout_test 2>&1 | tail -10
```

Both must pass.

- [ ] **Step 5: Commit**

```bash
git add tests/programs/recv_nonblock_test.c tests/programs/recv_timeout_test.c tests/test_programs.py
git commit -m "tests: add SO_RCVTIMEO blocking + non-blocking smoke tests"
```

---

## Phase 6 — Docs

### Task 12: Document `SYS_NET_SETSOCKOPT` + changelog

**Files:**
- Modify: `docs/syscalls.md`
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Update syscalls.md**

Add a new row for `SYS_NET_SETSOCKOPT` (24h), alphabetically with the other 2xh entries:

```
| 24h   | net_setsockopt | Set socket option: BX=fd, AL=option_name (SO_RCVTIMEO=1), ECX=value; returns AX=0 on success, -1 on bad fd / unknown opt / negative value, CF err |
```

Also update the `22h net_recvfrom` row's prose to mention that blocking behaviour is now controlled via SO_RCVTIMEO (not via a recvfrom argument).

- [ ] **Step 2: Update CHANGELOG.md**

Under Unreleased:

```markdown
- New syscall `SYS_NET_SETSOCKOPT` (24h) implementing `setsockopt(fd,
  option_name, value)`.  Currently supports `SO_RCVTIMEO` (option 1,
  value = milliseconds; 0 disables blocking).  `sys_net_recvfrom`
  reads the per-socket timeout on every call and blocks
  kernel-side via `hlt` until a matching packet arrives or the
  deadline elapses.  `dns` and `ping` adopt the new API and drop
  their userspace `sleep(1)` / `sleep(1000)` polling loops.
```

- [ ] **Step 3: Reflow**

```bash
python tools/wrap_md.py docs/syscalls.md docs/CHANGELOG.md
```

- [ ] **Step 4: Commit**

```bash
git add docs/syscalls.md docs/CHANGELOG.md
git commit -m "docs: document SYS_NET_SETSOCKOPT and SO_RCVTIMEO"
```

---

## Phase 7 — Full CI matrix

### Task 13: Run every workflow suite

Per the `feedback-run-full-ci-matrix-locally` memory: kernel-architecture changes (which a new syscall is) must run the full matrix locally before declaring done.

- [ ] **Step 1: Enumerate**

```bash
grep -nE "python.*tests/|pytest|tests/test_" .github/workflows/test.yml
```

- [ ] **Step 2: Run each suite**

Same list as the prior PR's Task 8 — typically `test_asm.py`, `test_bboefs.py`, `test_programs.py` (bbfs + ext2 + floppy + slow), `test_pipeline_*`, `test_cc_compatibility`, `test_cc_bits`, `test_archive`, `test_kernel_archive`, `pytest tests/unit/`, plus the smaller suites (`test_shell_chain`, `test_shell_history`, `test_scrollback`, `test_ps2`, `test_floppy_boot`, `test_draw`).

- [ ] **Step 3: Fix any failure**

If any suite fails, debug. Likely candidates:
- `test_archive` size drift → update the size table.
- `test_cc_compatibility` → bboeos.h missing the `setsockopt` prototype or `SO_RCVTIMEO` define.

---

## Phase 8 — Land

### Task 14: Push and open PR

- [ ] **Step 1: Push**

```bash
git push -u origin bboe/recv-timeout-setsockopt
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(net): SO_RCVTIMEO via SYS_NET_SETSOCKOPT" --body "$(cat <<'EOF'
## Summary

- New syscall `SYS_NET_SETSOCKOPT(fd, option_name, value)` (24h).  Currently supports one option: `SO_RCVTIMEO` (value = milliseconds; 0 disables blocking).
- `struct fd` gains a `recv_timeout_ms` field stored on the socket fd entry; `sys_net_recvfrom` reads it per call and blocks kernel-side via `sti; hlt; cli` (draining the NIC each PIT tick) until a matching packet arrives or `system_ticks >= deadline`.
- `recvfrom` keeps its 4-arg signature; default `recv_timeout_ms = 0` preserves today's non-blocking contract for every existing caller.
- `dns` and `ping` (both `.c` and archive `.asm`) call `setsockopt` once after `socket()` and drop their userspace `sleep(...)` polling loops.
- Two smoke tests: `recv_nonblock_test` guards the default-0 path; `recv_timeout_test` exercises the SO_RCVTIMEO blocking path.

Design: https://github.com/bboe/bboeos/blob/design-specs/2026-05-18-blocking-recvfrom-design.md
Plan: https://github.com/bboe/bboeos/blob/design-specs/2026-05-18-blocking-recvfrom-plan.md

Supersedes #411 (closed pre-merge; that PR put the timeout on the recvfrom argument list, deviating from POSIX).

## Test plan

- [x] `python tests/test_programs.py dns ping recv_nonblock_test recv_timeout_test`
- [x] Full local CI matrix (asm, bboefs, archive, programs bbfs/ext2/floppy, pipeline, cc compatibility, kernel archive, unit, etc.)

## Out of scope (per spec)

- `getsockopt`, `SO_SNDTIMEO`, other socket options.
- `select` / `poll` / `epoll`.
- Signal interruption of the wait (no EINTR).
- IRQ-driven NE2000 RX.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
