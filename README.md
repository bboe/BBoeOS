# BBoeOS design specs

This is an orphan branch (no shared history with `main`) where BBoeOS
design specs live. Specs are written here directly — usually via `git
mktree` + `git commit-tree` plumbing so the active feature worktree
isn't disturbed, or via a dedicated `git worktree` checkout of this
branch.

Each spec is a self-contained brainstorming-output design doc. When the
implementation plan grows complex enough to need its own document, it
lands here as `<date>-<topic>-plan.md` alongside the spec.

## Specs and plans

- [2026-05-15 — common utilities](./2026-05-15-common-utilities-design.md)
  — sort + sys_break + supporting cc.py changes. Landed across PRs #379–#382.
- [2026-05-16 — cc.py object files](./2026-05-16-cc-object-files-design.md)
  — ELF emission, `extern` declarations, `ccld` / `ccar`. In progress.
- [2026-05-16 — opendir / readdir](./2026-05-16-opendir-readdir-design.md)
  — POSIX directory iteration via Linux-style `getdents` + `<dirent.h>`.
  Plan: [2026-05-16-opendir-readdir-plan.md](./2026-05-16-opendir-readdir-plan.md).
  Status: design + plan complete; implementation pending.
- [2026-05-18 — blocking recvfrom](./2026-05-18-blocking-recvfrom-design.md)
  — `SO_RCVTIMEO` via a new `SYS_NET_SETSOCKOPT` syscall; kernel-side
  `hlt`-loop wait keyed on the per-fd timeout.  Replaces an earlier
  same-day design that put `timeout_ms` on the `recvfrom` argument
  list (PR #411, closed pre-merge).
  Plan: [2026-05-18-blocking-recvfrom-plan.md](./2026-05-18-blocking-recvfrom-plan.md).
  Status: design + plan complete; implementation pending.
- [2026-05-18 — cc.py bitfields + type casts](./2026-05-18-bitfields-cc-design.md)
  — bitfield struct members (`uint8_t name : N;`), type-cast expressions
  (`(T)expr`, `(T *)expr`), and conversion of all bit-twiddly drivers
  (NE2000, FDC, PIC, RTC, DMA, SB16, PS/2) to use the new syntax.
  Plan: [2026-05-18-bitfields-cc-plan.md](./2026-05-18-bitfields-cc-plan.md).
  Phase 2 plan revised after Phase 1 cc.py reconnaissance:
  [2026-05-18-bitfields-cc-plan-phase2.md](./2026-05-18-bitfields-cc-plan-phase2.md).
  Status: Phase 1 (casts) shipped in PR #422.  Phase 2 (bitfields)
  shipped in PR #425.  Phase 3 (driver conversions, batch 1): PR #428
  (ready for review) covers PIC IMR + NE2000.  Remaining drivers
  (FDC, RTC, DMA mode, SB16, PS/2) paused on the
  [stack-local structs](./2026-05-19-cc-local-structs-design.md) work
  to avoid retroactive rewrites.
- [2026-05-19 — cc.py stack-local struct values](./2026-05-19-cc-local-structs-design.md)
  — stack-local struct value declarations, arrays of struct locals,
  designated-field initializers (`= { .field = X }`), and a
  constant-fold + last-write-wins peephole pair for bitfield register
  init.  Enables the cleaner driver pattern and recovers the kernel
  size lost in PR #428.
  Plan: [2026-05-19-cc-local-structs-plan.md](./2026-05-19-cc-local-structs-plan.md).
  Status: design + plan complete; implementation pending.
- [2026-05-19 — tree reorg: kernel/, user/, ports/, tools/](./2026-05-19-tree-reorg-design.md)
  — pure mechanical rename so the ring boundary is visible at the top
  level (`kernel/` vs `user/`), upstream-wrapping ports get their own
  home (`ports/doom/`), and `tools/` shrinks to host-side build
  tooling only.  Lands before the shared-libc work so `user/libc/`
  arrives in its final location.
  Status: shipped in PR #437.
- [2026-05-20 — shared libbboeos: unify the vDSO + user/libc surfaces](./2026-05-20-shared-libbboeos-design.md)
  — promote `user/libc/` from "Doom-only static archive" to
  **libbboeos**, the shared BBoeOS system library mapped into every
  program.  Replaces the 13-entry hand-written `user/vdso/vdso.asm`
  with a real C source tree whose exports auto-populate
  `FUNCTION_POINTER_TABLE`.  cc.py user programs gain unknown-symbol
  → indirect-call fallback; per-program `strcmp` reimplementations
  go away.  Naming choice: "libbboeos" (not "libc") so future Rust /
  Go / Zig ports link against it without "I'm writing C?" friction.
  Seven-phase migration (source-dir rename → header cleanup →
  multi-page blob → cc.py extern fallback → stub archive → vDSO
  retirement → cc.py-compiles-libbboeos).
  Plan: [2026-05-24-va-arg-double-sizeof-expr-plan.md](./2026-05-24-va-arg-double-sizeof-expr-plan.md).
  Status: design + plan complete; implementation pending.
- [2026-05-24 — cc.py assignment as expression (parens required)](./2026-05-24-cc-assignment-as-expression-design.md)
  — accept assignments as expressions when wrapped in a dedicated
  pair of parentheses (`while ((p = next))`, `f((x = y))`).  Covers
  all eleven assignment operators across every existing lvalue
  shape, with result value in EAX and the lvalue's type.  The paren
  requirement is the feature, not a transitional restriction: it
  preserves the parser's lookahead simplicity and rules out the
  `if (x = y)` typo by construction.  Unblocks the `strcpy` idiom
  in `user/libc/string.c`.
  Plan: [2026-05-24-cc-assignment-as-expression-plan.md](./2026-05-24-cc-assignment-as-expression-plan.md).
  Status: shipped in PR #496.
- [2026-05-24 — cc.py va_arg(ap, double) + sizeof(expression)](./2026-05-24-va-arg-double-sizeof-expr-design.md)
  — two small features bundled: (1) `va_arg(ap, double)` advances
  the va-list cursor by 8 bytes instead of 4 (correct i386 cdecl
  semantics), unblocking `stdio.c`; (2) `sizeof(expression)` via
  a new `SizeofExpr` AST node + codegen-time `_expression_type`
  helper, unblocking `dirent.c`.
  Plan: [2026-05-24-va-arg-double-sizeof-expr-plan.md](./2026-05-24-va-arg-double-sizeof-expr-plan.md).
  Status: shipped in PR #497.
- [2026-05-25 — cc.py array of function pointers](./2026-05-25-cc-array-of-function-pointers-design.md)
  — three sub-features to unblock `stdlib.c`: (1) parse the
  `void (*name[N])(params)` declarator at file and local scope
  (plus the typedef path); (2) store to indexed elements
  (`arr[i] = fn`); (3) call through indexed elements
  (`arr[i]()`), via a new `IndexedCall` AST node.
  Plan: [2026-05-25-cc-array-of-function-pointers-plan.md](./2026-05-25-cc-array-of-function-pointers-plan.md).
  Status: design + plan complete; implementation pending.
- [2026-05-25 — cc.py GCC extended inline asm](./2026-05-25-cc-extended-inline-asm-design.md)
  — statement-level `__asm__ volatile("..." : outputs : inputs :
  clobbers)` with the full constraint set used by signal.c,
  syscall.c, and math.c: integer GP register constraints
  (`=a`/`+b`/`g`/`=&q`), x87 FP constraints (`=t`/`u`/`0`),
  memory output (`=m`), named operands, operand substitution
  (`%[name]`/`%b`/`%%`), and clobber lists.  Unblocks both
  remaining libbboeos files.
  Plan: [2026-05-25-cc-extended-inline-asm-plan.md](./2026-05-25-cc-extended-inline-asm-plan.md).
  Status: design + plan complete; implementation pending.
- [2026-05-27 — shell tab completion](./2026-05-27-shell-tab-completion-design.md)
  — Tab key in the shell line editor completes builtins + `bin/`
  entries in command position and files/directories (with trailing
  `/` on dirs) in argument position.  Single match inserts inline;
  multiple matches list bash-style under the prompt.  All logic
  lives in `user/programs/shell.c` using `open()` + `getdents()`.
  Plan: [2026-05-27-shell-tab-completion-plan.md](./2026-05-27-shell-tab-completion-plan.md).
  Status: shipped on branch `bboe/shell-tab-completion`.
