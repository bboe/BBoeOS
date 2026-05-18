# Blocking `recvfrom` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `timeout_ms` argument to `SYS_NET_RECVFROM` (passed in ESI). When `timeout_ms > 0`, the kernel spins on `sti; hlt; cli`, draining the NIC each iteration, until a matching packet arrives or `system_ticks` reaches the deadline. Collapse the userspace `sleep(1)` polling loops in `dns` and `ping` into the new argument.

**Architecture:** Kernel-side polled wait. No scheduler or wait-queue changes; the existing `kernel_yield` machinery stays a pipe-only concern. PIT runs at ~1 kHz; each `hlt` sleeps the CPU until the next IRQ (≤1 ms) at which point we re-drain the NIC. The `timeout_ms = 0` path is byte-for-byte the current non-blocking behaviour, preserving the carry-clear/AX=0 contract.

**Tech Stack:** cc.py (BBoeOS-flavour C with inline asm), NASM, QEMU integration tests under `tests/`.

**Spec:** [`2026-05-18-blocking-recvfrom-design.md`](./2026-05-18-blocking-recvfrom-design.md)

---

## Setup

### Task 0: Branch off main

**Files:** none (git only)

- [ ] **Step 1: Create the implementation branch**

```bash
cd /home/ubuntu/bboeos/.claude/worktrees/other
git fetch origin main
git checkout -B bboe/blocking-recvfrom origin/main
git log --oneline -1
```

Expected: HEAD points at the latest `main` commit (8a0901e8 or newer).

---

## Phase 1 — Plumbing: cc.py builtin + header

### Task 1: Update the `recvfrom` builtin to take 5 args

**Files:**
- Modify: `cc/codegen/x86/generator.py:130`
- Modify: `cc/codegen/x86/builtins.py:959-978`

The kernel side will not be touched yet — at the end of this task the build is broken (callers still pass 4 args). That's fine; the next two tasks make the build green again.

- [ ] **Step 1: Add `si` to the recvfrom pinned register set**

In `cc/codegen/x86/generator.py` at line 130, change:

```python
        "recvfrom": frozenset({"ax", "bx", "cx", "di", "dx"}),
```

to:

```python
        "recvfrom": frozenset({"ax", "bx", "cx", "di", "dx", "si"}),
```

- [ ] **Step 2: Update `builtin_recvfrom` to load ESI = `timeout_ms`**

In `cc/codegen/x86/builtins.py`, replace the body of `builtin_recvfrom` (lines 959–978) with:

```python
    def builtin_recvfrom(self, arguments: list[Node], /) -> None:
        """Generate code for the recvfrom() builtin.

        ``recvfrom(fd, buf, len, port, timeout_ms)`` emits register
        setup followed by ``mov ah, SYS_NET_RECVFROM / int 30h``.
        Returns bytes received in AX (0 if no matching packet and the
        timeout elapsed, or immediately if timeout_ms == 0).  Argument
        loads are topologically scheduled by
        :meth:`_emit_builtin_arg_moves` so a pinned-register variable
        referenced by any argument expression is not clobbered before
        use.
        """
        self._check_argument_count(arguments=arguments, expected=5, name="recvfrom")
        fd_argument, buffer_argument, len_argument, port_argument, timeout_argument = arguments
        self._emit_builtin_arg_moves([
            (self.target.bx_register, fd_argument),
            (self.target.di_register, buffer_argument),
            (self.target.count_register, len_argument),
            (self.target.dx_register, port_argument),
            (self.target.si_register, timeout_argument),
        ])
        self._emit_syscall("NET_RECVFROM")
        self.ax_clear()
```

- [ ] **Step 3: Verify `self.target.si_register` exists**

```bash
grep -n "si_register" cc/target.py
```

Expected: one or more matches showing `si_register` is a defined attribute of the x86 target (it is used by `sendto` and other builtins). If absent, find the correct attribute name (`source_register`, `esi_register`, etc.) used by `sendto` in `builtins.py` and substitute it in Step 2.

- [ ] **Step 4: Run the cc.py unit tests**

```bash
python -m pytest cc/tests/ -x -q
```

Expected: existing tests pass. The recvfrom callsites in `src/c/*.c` are still 4-arg and will fail to compile in later steps — but cc's own tests don't depend on those.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/generator.py cc/codegen/x86/builtins.py
git commit -m "cc: take timeout_ms as fifth recvfrom argument"
```

### Task 2: Update the bboeos.h compat header

**Files:**
- Modify: `tests/bboeos.h:135`

`tests/test_cc_compatibility.py` prepends this header to each `src/c/*.c` and compiles them with clang. Until the prototype matches the new arity, that test fails (per the `feedback-cc-compat-needs-header-decl` memory).

- [ ] **Step 1: Update the prototype**

In `tests/bboeos.h`, change line 135 from:

```c
int recvfrom(int fd, char *buffer, int length, int port);
```

to:

```c
int recvfrom(int fd, char *buffer, int length, int port, int timeout_ms);
```

- [ ] **Step 2: Verify alphabetical ordering is preserved**

```bash
grep -n "^[a-zA-Z_].*recvfrom\|^[a-zA-Z_].*read\|^[a-zA-Z_].*reboot\|^[a-zA-Z_].*rename\|^[a-zA-Z_].*rmdir" tests/bboeos.h
```

Expected: the surrounding declarations are still sorted (`reboot`, `recvfrom`, `rename`, …). If they aren't, fix the order in the same commit.

- [ ] **Step 3: Commit**

```bash
git add tests/bboeos.h
git commit -m "tests: declare 5-arg recvfrom in bboeos.h"
```

---

## Phase 2 — Kernel: hlt-loop wait

### Task 3: Rework `sys_net_recvfrom` to block until packet or deadline

**Files:**
- Modify: `src/syscall/syscalls.c:116-162`

The new flow:
1. Look up the fd; bail on bad fd.
2. Try `udp_receive` / `icmp_receive` once (preserves the fast-path code path).
3. If no match and `timeout_ms == 0`, return AX=0 (today's non-blocking behaviour).
4. Otherwise compute `deadline = rtc_tick_read() + timeout_ms` and loop:
   `sti; hlt; cli` → drain again → return on match or on deadline.

`rtc_tick_read` is the existing atomic reader in `src/drivers/rtc.c:241` (`ret`s with EAX = system_ticks, preserves all other registers). We can call it from C.

The dispatch path in `src/arch/x86/syscall.asm:406-414` already passes ESI through unchanged — no asm-side change needed.

- [ ] **Step 1: Add forward decls / extern at the top of `src/syscall/syscalls.c`**

Find the existing `extern uint8_t *net_receive_buffer;` (or similar) declarations near the recvfrom function. Add, alphabetically ordered with the other externs in that block:

```c
uint32_t rtc_tick_read();  // returns EAX = system_ticks atomically; preserves all other regs
```

Also add a tiny kernel helper for `sti; hlt; cli` right above `sys_net_recvfrom`:

```c
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

- [ ] **Step 2: Update the `sys_net_recvfrom` signature to take ESI**

Replace the function (currently lines 120–162) with:

```c
__attribute__((carry_return)) int
sys_net_recvfrom(int *bytes_copied __attribute__((out_register("ax"))),
                 int fd_num __attribute__((in_register("bx"))),
                 uint8_t *user_buffer __attribute__((in_register("edi"))),
                 int max_bytes __attribute__((in_register("ecx"))),
                 int local_port __attribute__((in_register("dx"))),
                 int timeout_ms __attribute__((in_register("esi")))) {
    struct fd *entry;
    uint8_t *payload;
    int payload_length;
    int dest_port;
    uint8_t *receive_buffer;
    uint32_t deadline;
    int have_deadline;
    if (!fd_lookup(fd_num, &entry)) {
        *bytes_copied = 0;
        return 1;
    }
    if (entry->type != FD_TYPE_UDP && entry->type != FD_TYPE_ICMP) {
        *bytes_copied = 0;
        return 1;
    }
    have_deadline = 0;
    deadline = 0;
    while (1) {
        if (entry->type == FD_TYPE_UDP) {
            if (udp_receive(&payload, &payload_length)) {
                receive_buffer = net_receive_buffer;
                dest_port = (receive_buffer[36] << 8) | receive_buffer[37];
                if (dest_port == (local_port & 0xFFFF)) {
                    if (payload_length > max_bytes) {
                        payload_length = max_bytes;
                    }
                    memcpy(user_buffer, payload, payload_length);
                    *bytes_copied = payload_length;
                    return 1;
                }
                // Non-matching UDP packet: drop and re-poll without
                // counting it against the deadline budget.
                continue;
            }
        } else {
            if (icmp_receive(&payload, &payload_length)) {
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
            deadline = rtc_tick_read() + (uint32_t)timeout_ms;
            have_deadline = 1;
        }
        if (rtc_tick_read() >= deadline) {
            *bytes_copied = 0;
            return 1;
        }
        kernel_hlt_idle();
    }
}
```

Key invariants:
- The `timeout_ms == 0` path returns from inside the loop *after* the first failed poll, exactly matching the pre-change behaviour (no `hlt`, no deadline computation).
- The deadline is computed lazily on the first miss, so the fast-path (packet already queued) costs zero `rtc_tick_read` calls.
- Non-matching UDP packets `continue` rather than `hlt` so a flood of foreign packets can't starve us — but they also don't extend the deadline (the cap is wall-clock, not packets-seen).
- Bad fd / wrong type returns AX=0 *without* blocking, preserving today's contract.

- [ ] **Step 3: Build the OS image**

```bash
./make_os.sh 2>&1 | tail -20
```

Expected: clean build. cc.py has compiled the new signature; NASM has linked. If `kernel_hlt_idle` collides with an existing symbol, rename to `net_hlt_idle`.

- [ ] **Step 4: Commit**

```bash
git add src/syscall/syscalls.c
git commit -m "syscall: block in sys_net_recvfrom until packet or timeout"
```

---

## Phase 3 — Userspace callsites

### Task 4: Drop the dns.c sleep-poll loop

**Files:**
- Modify: `src/c/dns.c:140-165` (the receive loop area)

- [ ] **Step 1: Read the current dns.c receive loop**

```bash
sed -n '140,170p' src/c/dns.c
```

This shows the 5000-iteration `sleep(1)` loop. Confirm the structure (loop counter, `recvfrom` call, success break, sleep, fall-through return).

- [ ] **Step 2: Replace the loop with a single blocking call**

Replace the loop with a single call:

```c
received = recvfrom(socket_fd, query_buffer, 512, 1024, 5000);
if (received == 0) {
    // Timeout — no DNS response within the budget.
    print_string("dns: no response\n");
    return 1;
}
```

Delete the wrapping `while` / `for` loop, the iteration counter, and the trailing `sleep(1)`. Keep the 5000 ms total budget (it matches the prior 5000-iteration × 1 ms cadence).

- [ ] **Step 3: Build and smoke-test in QEMU**

```bash
./make_os.sh
python tests/test_programs.py dns 2>&1 | tail -10
```

Expected: the `dns` test passes (resolves the QEMU-NAT-served name and prints the IP).

- [ ] **Step 4: Commit**

```bash
git add src/c/dns.c
git commit -m "dns: use blocking recvfrom in place of sleep(1) poll loop"
```

### Task 5: Drop the ping.c sleep-poll loop

**Files:**
- Modify: `src/c/ping.c:60-80` (initial reply check) and `src/c/ping.c:140-165` (main echo loop)

The current code has two recvfrom callsites:
- Line 71: probe / reply check used during socket bring-up.
- Line 146: per-echo reply check, paired with `sleep(1000)` at line 161.

- [ ] **Step 1: Update the per-echo loop**

Read `src/c/ping.c` around line 140–165. Replace the `recvfrom(fd, packet_buffer, 128, 0)` + `sleep(1000)` pair with:

```c
int n = recvfrom(fd, packet_buffer, 128, 0, 1000);
```

Delete the `sleep(1000)` line that previously paced the loop. The `1000` timeout (ms) replaces what was a 1 sec wall-clock wait + a one-shot probe.

- [ ] **Step 2: Update the bring-up recvfrom callsite**

At line 71, change:

```c
received = recvfrom(fd, query, 512, 1024);
```

to:

```c
received = recvfrom(fd, query, 512, 1024, 0);
```

The `0` preserves the existing non-blocking semantics — this callsite probes whether anything is already queued (no blocking intended).

- [ ] **Step 3: Build and smoke-test in QEMU**

```bash
./make_os.sh
python tests/test_programs.py ping 2>&1 | tail -10
```

Expected: `ping` passes — sends ICMP echo, gets reply, prints rtt.

- [ ] **Step 4: Commit**

```bash
git add src/c/ping.c
git commit -m "ping: use blocking recvfrom in place of sleep(1000) poll loop"
```

---

## Phase 4 — Regression coverage

### Task 6: Add a non-blocking smoke test

**Files:**
- Create: `tests/programs/recv_nonblock_test.c`
- Modify: `tests/test_programs.py` (add `ProgramTest` entry alphabetically)

Test programs live in `tests/programs/` and are auto-discovered by `make_os.sh` (line ~130: `find tests/programs -maxdepth 1 -name '*.c'`), so no build-system change is needed.

The risk we're guarding against is breaking the `timeout_ms = 0` carry-clear / AX=0 contract that `ping`'s bring-up probe and future callers rely on. `uptime()` returns seconds (see `src/c/uptime.c`) so a non-blocking call must complete within the same second it started.

- [ ] **Step 1: Write the test program**

Create `tests/programs/recv_nonblock_test.c`:

```c
/* recv_nonblock_test — verify recvfrom(timeout_ms=0) returns AX=0
   immediately when no matching packet is queued, and does NOT block
   long enough to cross a 1-second boundary.  Prints OK on success,
   FAIL: <reason> otherwise.  Run via `tests/test_programs.py
   recv_nonblock_test`; the harness boots the OS with `-device
   ne2k_isa` so socket creation succeeds. */
int main() {
    int file_descriptor;
    char buffer[64];
    int received;
    int before;
    int after;
    file_descriptor = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (file_descriptor < 0) {
        print_string("FAIL: socket\n");
        return 1;
    }
    before = uptime();
    received = recvfrom(file_descriptor, buffer, 64, 65000, 0);
    after = uptime();
    close(file_descriptor);
    if (received != 0) {
        print_string("FAIL: got bytes\n");
        return 1;
    }
    if (after != before) {
        print_string("FAIL: blocked\n");
        return 1;
    }
    print_string("OK\n");
    return 0;
}
```

(Local variables are spelled out — per the `feedback-no-abbreviations` memory, no `fd`, `buf`, `rc`.)

- [ ] **Step 2: Add the test_programs.py entry**

In `tests/test_programs.py`, locate the alphabetical insertion point — `recv_nonblock_test` sorts between `ping` (~line 863) and `recursive_exec_test`. Insert:

```python
    ProgramTest(
        "recv_nonblock_test",
        ["recv_nonblock_test"],
        r"^OK$",
        with_net=True,
    ),
```

(Copy the `with_net=True` flag from the neighbouring `dns` / `ping` entries — without it the harness boots without `-device ne2k_isa` and the `socket` call fails.)

- [ ] **Step 3: Build and run the test**

```bash
./make_os.sh
python tests/test_programs.py recv_nonblock_test 2>&1 | tail -10
```

Expected: PASS (the regex `^OK$` matches a line of the program output).

- [ ] **Step 4: Commit**

```bash
git add tests/programs/recv_nonblock_test.c tests/test_programs.py
git commit -m "tests: add non-blocking recvfrom smoke test"
```

---

## Phase 5 — Docs

### Task 7: Document the new ESI input + changelog

**Files:**
- Modify: `docs/syscalls.md`
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Update syscalls.md**

```bash
grep -n "SYS_NET_RECVFROM\|22h" docs/syscalls.md
```

In the `SYS_NET_RECVFROM` row, add ESI to the input register list with the description "timeout_ms (0 = non-blocking, >0 = max wait in ms; returns AX=0 on deadline)".

- [ ] **Step 2: Update CHANGELOG.md**

Under the `## Unreleased` section in `docs/CHANGELOG.md`, add:

```markdown
- `recvfrom` now takes a `timeout_ms` argument (`SYS_NET_RECVFROM` ESI).
  `0` preserves the previous non-blocking behaviour; `>0` blocks
  kernel-side via `hlt` until a matching packet arrives or the
  deadline elapses. `dns` and `ping` drop their userspace
  `sleep(1)` / `sleep(1000)` polling loops.
```

- [ ] **Step 3: Reflow long lines**

```bash
python tools/wrap_md.py docs/syscalls.md docs/CHANGELOG.md
```

- [ ] **Step 4: Commit**

```bash
git add docs/syscalls.md docs/CHANGELOG.md
git commit -m "docs: document timeout_ms on SYS_NET_RECVFROM"
```

---

## Phase 6 — Full CI matrix

### Task 8: Run every workflow suite locally

Per the `feedback-run-full-ci-matrix-locally` memory: this is a syscall-surface change with kernel-internal behaviour change. Run every suite in `.github/workflows/test.yml` before declaring done; don't stop at the dns/ping smoke tests.

- [ ] **Step 1: Enumerate the suites**

```bash
grep -n "python tests/\|pytest tests/\|tests/test_" .github/workflows/test.yml
```

Note every distinct invocation — typical entries include `test_asm.py`, `test_bboefs.py`, `test_programs.py` (bbfs + ext2), `test_pipeline.py`, `test_cc_compatibility.py`.

- [ ] **Step 2: Run each suite**

```bash
python tests/test_asm.py
python tests/test_bboefs.py
python tests/test_programs.py
python tests/test_programs.py --filesystem ext2
python tests/test_pipeline.py 2>/dev/null   # if present
python -m pytest tests/test_cc_compatibility.py
```

(Adjust to match the actual list discovered in Step 1. The 2 KB-block-size ext2 matrix runs automatically inside `test_programs.py --filesystem ext2`.)

Expected: every suite green.

- [ ] **Step 3: If any suite fails, debug and fix**

Use `superpowers:systematic-debugging` if the failure isn't obvious. Each fix lands as its own commit on this branch.

- [ ] **Step 4: Final status check**

```bash
git log --oneline origin/main..HEAD
git status
```

Expected: clean tree, the eight task commits ordered as above.

---

## Phase 7 — Land the plan doc

### Task 9: Push this plan to `design-specs`

**Files:**
- Add: `2026-05-18-blocking-recvfrom-plan.md` on `design-specs`
- Modify: `README.md` on `design-specs` (link the plan from the existing spec entry)

Per the `feedback-specs-design-branch` memory, the plan lives next to the spec on the `design-specs` orphan branch — never on this feature branch.

- [ ] **Step 1: Build the new design-specs commit via plumbing**

```bash
cd /home/ubuntu/bboeos
git fetch origin design-specs
PLAN=/path/to/this/plan/file.md
README=/tmp/specs-readme-updated.md
# Pull current README, update the 2026-05-18 bullet to link the plan.
git show origin/design-specs:README.md > "$README"
# Hand-edit $README to add: "Plan: [2026-05-18-blocking-recvfrom-plan.md](...). Status: ..."
BASE_TREE=$(git rev-parse origin/design-specs^{tree})
PLAN_BLOB=$(git hash-object -w "$PLAN")
README_BLOB=$(git hash-object -w "$README")
{
  git ls-tree "$BASE_TREE" | grep -v -E "(README\.md|2026-05-18-blocking-recvfrom-plan\.md)$"
  printf '100644 blob %s\tREADME.md\n' "$README_BLOB"
  printf '100644 blob %s\t2026-05-18-blocking-recvfrom-plan.md\n' "$PLAN_BLOB"
} > /tmp/tree-entries
NEW_TREE=$(git mktree < /tmp/tree-entries)
NEW_COMMIT=$(git commit-tree "$NEW_TREE" -p "$(git rev-parse origin/design-specs)" \
              -m "Add 2026-05-18 blocking-recvfrom plan")
git push origin "$NEW_COMMIT":refs/heads/design-specs
```

- [ ] **Step 2: Verify on GitHub**

Open the `design-specs` branch in the GitHub web UI; confirm the plan file is listed and the README links to it.

---

## Phase 8 — PR

### Task 10: Open the PR against main

- [ ] **Step 1: Push the feature branch**

```bash
git push -u origin bboe/blocking-recvfrom
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --title "feat(net): blocking recvfrom with timeout" --body "$(cat <<'EOF'
## Summary

- `SYS_NET_RECVFROM` (22h) gains an ESI input: `timeout_ms`. `0` keeps today's non-blocking behaviour; `>0` blocks kernel-side (`sti; hlt; cli` loop draining the NIC each PIT tick) until a matching packet arrives or the deadline elapses.
- `dns` and `ping` drop their userspace `sleep(1)` / `sleep(1000)` polling loops in favour of the new timeout argument.
- `recvfrom` userspace prototype goes from 4 to 5 args; `tests/bboeos.h`, the cc.py builtin, and both in-tree callers are updated in lock-step.

Design: https://github.com/bboe/bboeos/blob/design-specs/2026-05-18-blocking-recvfrom-design.md
Plan: https://github.com/bboe/bboeos/blob/design-specs/2026-05-18-blocking-recvfrom-plan.md

## Test plan

- [x] `python tests/test_programs.py dns` — dns still resolves
- [x] `python tests/test_programs.py ping` — ping still echoes
- [x] `python tests/test_programs.py recv_nonblock_test` — non-blocking contract regression
- [x] Full `.github/workflows/test.yml` matrix run locally (asm, bboefs, programs bbfs, programs ext2, pipeline, cc compatibility)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Wait for CI**

Watch `gh pr checks` until every check passes. Address any failures in additional commits on the branch (don't force-push to clean up; create new commits).
