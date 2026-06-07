# BBoeOS design specs

This is an orphan branch (no shared history with `main`) where BBoeOS design
specs live. Specs are written here directly — usually via `git mktree` + `git
commit-tree` plumbing so the active feature worktree isn't disturbed, or via a
dedicated `git worktree` checkout of this branch.

Each spec is a self-contained brainstorming-output design doc. When the
implementation plan grows complex enough to need its own document, it lands here
as `<date>-<topic>-plan.md` alongside the spec.

## Specs and plans

- [2026-05-15 — common utilities](./2026-05-15-common-utilities-design.md) —
  sort + sys_break + supporting cc.py changes. Landed across PRs #379–#382.
- [2026-05-16 — cc.py object files](./2026-05-16-cc-object-files-design.md) —
  ELF emission, `extern` declarations, `ccld` / `ccar`.  Status: shipped —
  object/`extern` mode in PR #379, the `ccld` / `ccar` linker + archiver in PR
  #384.
- [2026-05-16 — opendir / readdir](./2026-05-16-opendir-readdir-design.md) —
  POSIX directory iteration via Linux-style `getdents` + `<dirent.h>`. Plan:
  [2026-05-16-opendir-readdir-plan.md](./2026-05-16-opendir-readdir-plan.md).
  Status: shipped — kernel `getdents` (`SYS_IO_GETDENTS`) in PR #385, the libc
  `opendir` / `readdir` / `closedir` / `rewinddir` in PR #392.
- [2026-05-18 — blocking recvfrom](./2026-05-18-blocking-recvfrom-design.md) —
  `SO_RCVTIMEO` via a new `SYS_NET_SETSOCKOPT` syscall; kernel-side `hlt`-loop
  wait keyed on the per-fd timeout.  Replaces an earlier same-day design that
  put `timeout_ms` on the `recvfrom` argument list (PR #411, closed pre-merge).
  Plan:
  [2026-05-18-blocking-recvfrom-plan.md](./2026-05-18-blocking-recvfrom-plan.md).
  Status: shipped in PR #414.
- [2026-05-18 — cc.py bitfields + type
  casts](./2026-05-18-bitfields-cc-design.md) — bitfield struct members
  (`uint8_t name : N;`), type-cast expressions (`(T)expr`, `(T *)expr`), and
  conversion of all bit-twiddly drivers (NE2000, FDC, PIC, RTC, DMA, SB16, PS/2)
  to use the new syntax. Plan:
  [2026-05-18-bitfields-cc-plan.md](./2026-05-18-bitfields-cc-plan.md). Phase 2
  plan revised after Phase 1 cc.py reconnaissance:
  [2026-05-18-bitfields-cc-plan-phase2.md](./2026-05-18-bitfields-cc-plan-phase2.md).
  Status: complete.  Phase 1 (casts) shipped in PR #422.  Phase 2 (bitfields)
  shipped in PR #425.  Phase 3 (driver conversions): PR
  #428 covered PIC IMR + NE2000; FDC, DMA mode, SB16, and PS/2 followed
  once the [stack-local structs](./2026-05-19-cc-local-structs-design.md) work
  landed.  RTC was the last holdout — its lone update-in-progress poll now reads
  through `struct cmos_status_a` instead of a bare 0x80 mask.
- [2026-05-19 — cc.py stack-local struct
  values](./2026-05-19-cc-local-structs-design.md) — stack-local struct value
  declarations, arrays of struct locals, designated-field initializers (`= {
  .field = X }`), and a constant-fold + last-write-wins peephole pair for
  bitfield register init.  Enables the cleaner driver pattern and recovers the
  kernel size lost in PR #428. Plan:
  [2026-05-19-cc-local-structs-plan.md](./2026-05-19-cc-local-structs-plan.md).
  Status: shipped in PR #430.
- [2026-05-19 — tree reorg: kernel/, user/, ports/,
  tools/](./2026-05-19-tree-reorg-design.md) — pure mechanical rename so the
  ring boundary is visible at the top level (`kernel/` vs `user/`),
  upstream-wrapping ports get their own home (`ports/doom/`), and `tools/`
  shrinks to host-side build tooling only.  Lands before the shared-libc work so
  `user/libc/` arrives in its final location. Status: shipped in PR #437.
- [2026-05-20 — shared libbboeos: unify the vDSO + user/libc
  surfaces](./2026-05-20-shared-libbboeos-design.md) — promote `user/libc/` from
  "Doom-only static archive" to **libbboeos**, the shared BBoeOS system library
  mapped into every program.  Replaces the 13-entry hand-written
  `user/vdso/vdso.asm` with a real C source tree whose exports auto-populate
  `FUNCTION_POINTER_TABLE`.  cc.py user programs gain unknown-symbol →
  indirect-call fallback; per-program `strcmp` reimplementations go away.
  Naming choice: "libbboeos" (not "libc") so future Rust / Go / Zig ports link
  against it without "I'm writing C?" friction. Seven-phase migration
  (source-dir rename → header cleanup → multi-page blob → cc.py extern fallback
  → stub archive → vDSO retirement → cc.py-compiles-libbboeos). Plan:
  [2026-05-24-va-arg-double-sizeof-expr-plan.md](./2026-05-24-va-arg-double-sizeof-expr-plan.md).
  Status: shipped — the seven-phase migration completed across PRs
  #503–#507, dropping clang from the build (core now needs only nasm
  + python3).
- [2026-05-24 — cc.py assignment as expression (parens
  required)](./2026-05-24-cc-assignment-as-expression-design.md) — accept
  assignments as expressions when wrapped in a dedicated pair of parentheses
  (`while ((p = next))`, `f((x = y))`).  Covers all eleven assignment operators
  across every existing lvalue shape, with result value in EAX and the lvalue's
  type.  The paren requirement is the feature, not a transitional restriction:
  it preserves the parser's lookahead simplicity and rules out the `if (x = y)`
  typo by construction.  Unblocks the `strcpy` idiom in `user/libc/string.c`.
  Plan:
  [2026-05-24-cc-assignment-as-expression-plan.md](./2026-05-24-cc-assignment-as-expression-plan.md).
  Status: shipped in PR #496.
- [2026-05-24 — cc.py va_arg(ap, double) +
  sizeof(expression)](./2026-05-24-va-arg-double-sizeof-expr-design.md) — two
  small features bundled: (1) `va_arg(ap, double)` advances the va-list cursor
  by 8 bytes instead of 4 (correct i386 cdecl semantics), unblocking `stdio.c`;
  (2) `sizeof(expression)` via a new `SizeofExpr` AST node + codegen-time
  `_expression_type` helper, unblocking `dirent.c`. Plan:
  [2026-05-24-va-arg-double-sizeof-expr-plan.md](./2026-05-24-va-arg-double-sizeof-expr-plan.md).
  Status: shipped in PR #497.
- [2026-05-25 — cc.py array of function
  pointers](./2026-05-25-cc-array-of-function-pointers-design.md) — three
  sub-features to unblock `stdlib.c`: (1) parse the `void (*name[N])(params)`
  declarator at file and local scope (plus the typedef path); (2) store to
  indexed elements (`arr[i] = fn`); (3) call through indexed elements
  (`arr[i]()`), via a new `IndexedCall` AST node. Plan:
  [2026-05-25-cc-array-of-function-pointers-plan.md](./2026-05-25-cc-array-of-function-pointers-plan.md).
  Status: shipped in PR #499.
- [2026-05-25 — cc.py GCC extended inline
  asm](./2026-05-25-cc-extended-inline-asm-design.md) — statement-level `__asm__
  volatile("..." : outputs : inputs : clobbers)` with the full constraint set
  used by signal.c, syscall.c, and math.c: integer GP register constraints
  (`=a`/`+b`/`g`/`=&q`), x87 FP constraints (`=t`/`u`/`0`), memory output
  (`=m`), named operands, operand substitution (`%[name]`/`%b`/`%%`), and
  clobber lists.  Unblocks both remaining libbboeos files. Plan:
  [2026-05-25-cc-extended-inline-asm-plan.md](./2026-05-25-cc-extended-inline-asm-plan.md).
  Status: shipped in PR #500.
- [2026-05-27 — shell tab
  completion](./2026-05-27-shell-tab-completion-design.md) — Tab key in the
  shell line editor completes builtins + `bin/` entries in command position and
  files/directories (with trailing `/` on dirs) in argument position.  Single
  match inserts inline; multiple matches list bash-style under the prompt.  All
  logic lives in `user/programs/shell.c` using `open()` + `getdents()`. Plan:
  [2026-05-27-shell-tab-completion-plan.md](./2026-05-27-shell-tab-completion-plan.md).
  Status: shipped on branch `bboe/shell-tab-completion`.
- [2026-06-01 — cc.py rep-string loop
  recognition](./2026-06-01-cc-rep-string-loops-design.md) — recognize
  hand-written element-wise init/copy loops (`for (i=0;i<n;i++) dst[i]=0;` /
  `dst[i]=src[i];`) and rewrite them into `rep stos{b,w,d}` / `rep movs{b,w,d}`
  via a new IR pass over natural loops (reusing the strength-reduction
  induction-variable analysis) plus a `RepString` IR node.  Adds `movsd` /
  `stosd` to the self-hosted assembler.  Backlog item #3. Plan:
  [2026-06-01-cc-rep-string-loops-plan.md](./2026-06-01-cc-rep-string-loops-plan.md).
  Status: implemented on branch `bboe/cc-rep-string-loops` (PR #566).
- [2026-06-01 — cc.py `Place`
  refactor](./2026-06-01-cc-place-refactor-design.md) — unify the ~15-node
  access-expression zoo (`Index`, `MemberAccess`, `IndexMember*`, `Deref*`,
  `DoubleIndex`, …) behind a single recursive `Place` (addressable-location) AST
  plus five `Place*` operation nodes and a recursive `_resolve_place` address
  core, so chained accesses like `a[i][j].f[k]` compose for free.  Includes the
  IR-vs-direct-emission rationale (why `Index` is in the IR but member access
  isn't). Plan 1:
  [2026-06-01-cc-place-refactor-plan.md](./2026-06-01-cc-place-refactor-plan.md).
  Plan 2:
  [2026-06-01-cc-place-refactor-plan2.md](./2026-06-01-cc-place-refactor-plan2.md).
  Plan 3:
  [2026-06-02-cc-place-refactor-plan3.md](./2026-06-02-cc-place-refactor-plan3.md).
  Plan 4:
  [2026-06-02-cc-place-refactor-plan4.md](./2026-06-02-cc-place-refactor-plan4.md).
  Decision spike → IR design:
  [2026-06-02-cc-place-refactor-decision-spike.md](./2026-06-02-cc-place-refactor-decision-spike.md)
  — chose option (b): grow the IR, retire the Block escape hatch entirely;
  byte-EFFICIENCY gate. Staged program (Stages 1-4) supersedes the single Plan
  5. Plan 5 / Stage 1:
  [2026-06-02-cc-place-refactor-plan5-stage1.md](./2026-06-02-cc-place-refactor-plan5-stage1.md)
  — foundation: per-function byte-size differential gate + carve
  `PlaceLoad`/`PlaceStore`/`PlaceCall` off `Block` into a new `ir.Access` op,
  treated identically-conservatively at every `ir.Block` site. Byte-identical by
  design. (Merged, PR #578.) Plan 5 / Stage 2 design:
  [2026-06-02-cc-place-refactor-plan5-stage2-design.md](./2026-06-02-cc-place-refactor-plan5-stage2-design.md)
  — **MERGED INTO STAGE 3** (not a standalone stage). Operand lowering's
  consumers (CSE/LICM/SSA) all live in Stage 3, so lowering alone in a Stage 2
  is pure byte cost that fails the gate; the two real Stage-2 wins
  (copy-prop-into-access, dead-pure-load DCE) need only the substitute primitive
  over the existing AST. Its operand model + primitives + analysis feed the
  Stage 3 design. Plan 5 / Stage 3a design:
  [2026-06-02-cc-place-refactor-plan5-stage3a-design.md](./2026-06-02-cc-place-refactor-plan5-stage3a-design.md)
  — **SCOPE EXPANDED** (codegen-only design partially superseded). Recursive
  `resolve_address` over a generalized `MemOperand` (base =
  label|frame|register), deref-breaks-the-chain, bitfields/width at the
  terminal, retire the double-index hack — Clang `EmitLValue` / GCC
  `get_inner_reference` model. Implementation found the **parser** caps depth
  (no 2D arrays / pointer-to-array / triple subscript; member-index size 1–2
  only), so arbitrary depth can't land end-to-end without parser+type-system
  work. Expanded to a full multidimensional-array language feature (parser +
  types + codegen); resolver architecture + Task 1 (`MemOperand` skeleton,
  committed on `bboe/cc-place-plan5-stage3a`, inert) survive as the codegen
  sub-piece. Plan 5 / Stage 3a plan:
  [2026-06-02-cc-place-refactor-plan5-stage3a.md](./2026-06-02-cc-place-refactor-plan5-stage3a.md)
  — **SUPERSEDED beyond Task 1** by the scope expansion. Codegen-only 9-task
  migration (build `MemOperand`+`resolve_address`, migrate one Place shape per
  task, gate, delete each bespoke emitter). Task 1 landed; the rest is
  re-planned under the expanded multidimensional-array feature design
  (forthcoming). Multidim arrays design kickoff:
  [2026-06-02-cc-multidim-arrays-design-kickoff.md](./2026-06-02-cc-multidim-arrays-design-kickoff.md)
  — checkpoint capturing the expanded-scope decisions (Stage 3a → multidim-array
  feature; split A=pointer-chain depth+resolver unification / B=true multidim
  arrays; **B chosen first** with a **structured `Type` object**) and the open
  design axes (type-model shape, migration strategy, parser, layout/stride,
  sizeof, decay, codegen). Design not started — to begin in a dedicated session.
  Task 1 (`MemOperand`+`resolve_address` skeleton) preserved on
  `bboe/cc-place-plan5-stage3a`. Multidim B / Stage 4 contiguous-codegen plan:
  [2026-06-03-cc-multidim-contiguous-codegen-plan.md](./2026-06-03-cc-multidim-contiguous-codegen-plan.md)
  — end-to-end contiguous multidim arrays (local+global, 2-D/3-D,
  load/store/sizeof): register a structured `ArrayType`, row-major storage +
  recursive `sizeof` (+ the `unsigned short` stride fix), a uniform nested
  `SubscriptPlace` parser shape, and codegen dispatch on the base's registered
  type — contiguous multidim → row-major; array-of-pointers → reconstruct the
  legacy node → existing emitter, byte-identical. Declaration-parsing stage
  merged in PR #579; pointer-to-array / struct-multidim-fields / multidim-params
  deferred. Multidim B / initializers plan:
  [2026-06-03-cc-multidim-initializers-plan.md](./2026-06-03-cc-multidim-initializers-plan.md)
  — nested `{{1,2,3},{4,5,6}}` and flat multidim array initializers, local +
  global, scalar elements, with zero-fill; lifts the two initializer guards.
  First of three deferred-work chunks after the Stage 4 merge (PR #580):
  initializers → struct fields → pointer-to-array/params. Multidim B /
  struct-fields plan:
  [2026-06-03-cc-multidim-struct-fields-plan.md](./2026-06-03-cc-multidim-struct-fields-plan.md)
  — multidim array struct fields `struct { int cells[2][3]; }` + access
  `g.cells[i][j]` / `p->cells[i][j]`: nested subscript after a member, multidim
  field layout/size, row-major member access via the multidim machinery rooted
  at the field offset. Chunk 2 of 3 (initializers PR #581 merged). Multidim B /
  pointer-to-array + params plan:
  [2026-06-03-cc-pointer-to-array-plan.md](./2026-06-03-cc-pointer-to-array-plan.md)
  — `int (*p)[3]` and passing multidim arrays to functions (params +
  array→pointer decay): a structured-type side dict
  (`PointerType(ArrayType(...))`), subscript = load-pointer-then-row-major
  (deref-breaks-the-chain), lifts the last (param) guard. Chunk 3 of 3 (struct
  fields PR #582 merged). Plan 5 / Stage 3a plan (re-scoped):
  [2026-06-02-cc-place-refactor-plan5-stage3a.md](./2026-06-02-cc-place-refactor-plan5-stage3a.md)
  — **COMPLETE (PR #585).** After the multidim feature landed, 3a re-scoped to
  the codegen-only resolver unification: one recursive `resolve_address(place)
  -> MemoryOperand`, eleven bespoke place-emitters retired, multidim
  constant-index fold, byte-gated. (Separate asm.c 3-operand `imul` self-host
  fix: PR #584.) Deferred follow-up: the `unsigned short *` width-gap family
  (own byte-changing PR). Plan 5 / Stage 3b design:
  [2026-06-03-cc-place-refactor-plan5-stage3b-design.md](./2026-06-03-cc-place-refactor-plan5-stage3b-design.md)
  — **BLOCKED on a register-allocator prerequisite (see below).** Operand
  lowering: introduce optimizer-visible `Address`/`Load`/`Store`/`AddressOf`
  ops, fold the **full** access family off `Block`/`Access`, fold + **delete**
  `ir.Index`/`ir.IndexAssign`, **rewrite the rep-string matcher** onto the
  uniform shapes (two gated PRs 3b.1/3b.2); CSE/LICM/strength-reduction over
  `Address` is Stage 3c. **Finding (2026-06-03):** cc.py's x86 backend has no
  register allocator — every IR temp spills to a frame slot — so lifting access
  leaves to IR `Value`s adds spill/reload pairs and fails the byte gate (cc.py's
  own Stage-2 design predicted this: "operand lowering alone … pure byte cost
  that fails the gate"). Decision: build a register allocator first (its own
  design + plan; forthcoming), then 3b/3c resume on register-resident temps.
  Register allocator design:
  [2026-06-03-cc-register-allocator-design.md](./2026-06-03-cc-register-allocator-design.md)
  — the Stage-3b prerequisite. Replace cc.py's heuristic AST auto-pin with one
  **unified graph-coloring (Chaitin-Briggs)** allocator over the **flat IR + a
  new CFG-level liveness** pass, assigning register/spill *homes* to locals,
  params, **and `_ir_*` temps** uniformly (emission stays accumulator-based, AX
  = scratch). Precolor the hard constraints (AX, regparm `EAX/EDX/ECX`,
  call-clobbers, 16-bit `SI/DI`-index legality, byte-alias limits,
  `BP`-only-in-`main`); keep the existing clobber-cost economics as coloring's
  soft-cost/spill gate; coalesce the `mov reg,eax` move-outs to erase temp
  spills. Four gated PRs: (1) engine unwired/byte-neutral, (2) switch
  locals/params to it + delete auto-pin (parity step, ≤ baseline — dominant
  risk), (3) extend to IR temps (expect byte decreases), (4) cleanup. Builds on
  the existing `LivenessAnalyzer` + interference + `pinned_register` emission
  wiring + SSA/CFG layer. Register allocator / PR 1 plan:
  [2026-06-03-cc-register-allocator-pr1-plan.md](./2026-06-03-cc-register-allocator-pr1-plan.md)
  — the **unwired engine**: a pure `cc/regalloc.py` with IR-level
  liveness/interference over `cc.cfg` + a cost-aware Chaitin-Briggs colorer
  (def/use → interference → conservative coalescing → simplify → optimistic
  spill → soft-cost select with the auto-pin benefit gate). Target-specific
  constraints/costs are caller-supplied parameters (synthetic in unit tests);
  nothing in `cc/codegen` imports it, so the PR is byte-neutral by construction.
  8 TDD tasks, `tests/unit/test_cc_regalloc.py`. PR 2 (wire locals, parity gate)
  gets its own plan. Register allocator / PR 2 design:
  [2026-06-03-cc-register-allocator-pr2-design.md](./2026-06-03-cc-register-allocator-pr2-design.md)
  — the **parity step**: make `cc/regalloc.py` the single authority for
  **locals/params** homes and **delete** the auto-pin heuristic (`_ir_*` temps
  still spill — that is PR 3). Keep the AST-derived economics (ref counts →
  `spill_benefit`, `register_clobber_counts` minus pre-store elision →
  `register_save_cost`) as the engine's cost inputs; color over the IR-CFG
  interference; map `Allocation.homes` → the existing `pinned_register` wiring.
  Parity policy: aim for full parity, **re-bless** only small
  individually-justified stragglers; convergence evidence is a **golden `{name:
  register}` snapshot** of the heuristic captured before deletion. 6-step
  cutover (extract counting helpers → freeze golden → input adapter → wire
  behind flag → converge → flip + delete). PR 3 (IR temps) and PR 4 (cleanup)
  get their own plans. Register allocator / PR 2 plan:
  [2026-06-03-cc-register-allocator-pr2-plan.md](./2026-06-03-cc-register-allocator-pr2-plan.md)
  — 7 TDD tasks: (1) extract `AutoPinEconomics` from the heuristic
  (byte-neutral), (2) freeze the per-function `{var:register}` golden +
  `tests/test_cc_register_homes.py`, (3) pure
  `cc/codegen/x86/regalloc_inputs.py` adapter (economics →
  `CostModel`/`RegisterConstraints`/interference) + unit tests, (4) wire
  `regalloc.color()` behind `BBOE_REGALLOC`, (5) converge to byte parity (gate +
  golden are the oracles; document/re-bless stragglers), (6) flip default +
  delete `_select_auto_pin_candidates` & helpers, (7) full local CI matrix +
  finish. Key call: `color()` is fed AST `LivenessAnalyzer` interference (works
  for every function incl. `main`), `moves=set()`. Native-Address emission /
  expected byte reductions:
  [2026-06-06-cc-native-address-emission-expected-byte-reductions.md](./2026-06-06-cc-native-address-emission-expected-byte-reductions.md)
  — the **measured ledger** behind PR #590's wall: every byte regression
  observed admitting the remaining store RHS/index classes (`BinaryOperation`
  +21, `Cast` +6 width-lossy, `Index` +2, compound-index leaf stores +6/+12),
  each with root cause (IR temps live across the opaque legacy address walk;
  width dropped by the `Value` round-trip) and the checkable claim that the
  native-Address emission refactor erases it — plus the beyond-parity 3c
  reductions (Address CSE, LICM, Horner re-use) and the two risks
  (addressing-mode preservation, CSE cost model). Each entry is a
  re-admit-and-gate verification step for the refactor design below.
  Native-Address emission design:
  [2026-06-06-cc-native-address-emission-design.md](./2026-06-06-cc-native-address-emission-design.md)
  — the approved refactor design (Approach C, two-tier): `ir.Address` stays
  shape-bearing; a codegen-entry **AddressPlanner** lowers each one to a
  target-aware `AddressPlan` (base / `(value, scale)` terms split SIB-encodable
  vs needs-arithmetic / folded displacement / width+bitfield+decay facts);
  emission materializes plans natively (AST re-seat narrowed to a census-locked
  residual path — see the phase-1 errata in the doc), `ir.Store` gains a `width`
  override stamped from `Cast` at lowering, and regalloc consumes per-Address
  clobber sets. Subsumes Stage 3b.2 (delete `ir.Index`/`ir.IndexAssign`, rep
  matchers on uniform shapes, bare-`Var` increments) and Stage 3c (CSE/Horner +
  LICM over plans via fold-vs-materialize on the Address destination temp,
  no-alias-analysis invalidation, explicit cost model that never materializes
  single-term SIB plans). Five gated phases: flip-over 0-delta → ledger
  re-admissions (classes 1/3/4 then 2) → 3b.2 0-delta → 3c shrinks with baseline
  refreshes. Native-Address emission / phase 1 plan:
  [2026-06-06-cc-native-address-emission-phase1-plan.md](./2026-06-06-cc-native-address-emission-phase1-plan.md)
  — 11-task TDD plan for the flip-over: AddressPlan/AddressTerm dataclasses →
  extract `_store_accumulator_to_local` → per-shape-class planner+materializer
  cutover (dot members → arrow/chained bases → member stores →
  single-dynamic-index → multidim/Horner/mixed chains →
  AddressOf/IncrementDecrement/IndirectCall) → totality + delete the AST re-seat
  → declared clobber facts → full local CI matrix. Core strategy: **split, don't
  duplicate** — every mixed legacy helper is refactored in place into a
  pure-derivation half (feeds the planner) and an emitting half (feeds the
  materializer), so byte parity holds by construction; the byte gate (zero
  GREW/shrank), `cc_place` golden, and `cc_bits` 16/32 run after every task.
  Native-Address emission / phase 2 plan:
  [2026-06-07-cc-native-address-emission-phase2-plan.md](./2026-06-07-cc-native-address-emission-phase2-plan.md)
  — 9-task TDD plan for the clobber-aware re-admissions (ledger classes 1/3/4):
  per-instruction live-across export from `build_interference` → eager
  per-function Address planning before IR-temp coloring → plan clobbers +
  terminal-owned extras (subscript BX guard; conservative residual set) into
  `RegisterConstraints.allowed` → div/mod EDX modeling + remainder-fusion
  preservation → accumulator-aware single-use pinning across folded Address
  phantoms → re-admit `Index` / `BinaryOperation` store RHS and compound-index
  stores (register-direct term accumulation), each gated 0-delta-or-shrank on
  the ledger's named functions. Staged — Plan 1 = `Place` infra + the
  `IndexMember*` family as a byte-exact proof-of-concept, with the IR-touching
  `Index` fold sequenced last behind a decision spike.  Status: Plan 1 shipped
  (PR
  #573); Plan 2 (the `Member*` family) planned.
