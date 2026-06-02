# cc.py `Place` Refactor — Decision Spike → Design: grow the IR, retire `Block` entirely

**Status:** Decided. The earlier draft of this spike leaned toward option (a)
(keep the small IR, map the subscript Place shape back to `ir.Index`) on
risk/effort grounds. After weighing it against how production compilers
actually work, and with the acceptance gate relaxed from *byte-identical* to
*byte-efficient*, the decision is **option (b): lower all `Place` accesses into
first-class IR ops and retire the `Block` escape hatch entirely.**

This supersedes the (a) recommendation. It turns Plan 5 from a single plan into
a small **staged program** (below).

## Why (b), and why retire `Block`

`Block(node=<arbitrary AST>)` is "lower this AST node via the existing
statement codegen" — an escape hatch that routes most access shapes (and
declarations, struct-init, inline asm, special-cased assigns) *around* the IR
and its optimizer. No production compiler has an equivalent:

- **Clang/LLVM:** the AST lvalue tree (`ArraySubscriptExpr` / `MemberExpr` /
  deref) is lowered by `EmitLValue` into a CodeGen-time `LValue` (an address),
  then to **uniform** LLVM IR — `getelementptr` + `load`/`store`. All
  optimization (LICM, GVN, the loop-idiom recognizer that is the analog of our
  rep-string pass) runs on that uniform IR. No escape hatch.
- **GCC:** GENERIC lvalue trees (`ARRAY_REF` / `COMPONENT_REF` / `MEM_REF` /
  `ADDR_EXPR` — almost exactly our `SubscriptPlace`/`MemberPlace`/
  `DereferencePlace`/`PlaceAddressOf`) are carried as *structured references in
  the optimizable IR* (GIMPLE); passes call `get_inner_reference` to decompose
  a ref into `(base, offset, bitpos)` — precisely what our `_resolve_place`
  returns. Lowered to flat addressing later.

The part of Plans 1–4 that already matches the big compilers is
`_resolve_place` (≈ `EmitLValue` / `get_inner_reference`). The part that does
**not** is the `Block` escape hatch. (b) removes it; accesses become a uniform,
optimizer-visible IR representation; `_resolve_place` becomes the shared
address core that *emission* (and the IR lowering) use.

Inline asm is **not** an obstacle: it does not need `Block`. It becomes a
dedicated **opaque** IR op (`ir.InlineAsm` already exists and the bare
`asm("…")` form already lowers to it), exactly as LLVM (inline-asm `CallInst`
with `sideeffect`) and GCC (`GIMPLE_ASM`) represent it — a barrier the
optimizer won't reorder/eliminate/propagate across, with declared clobbers
informing liveness. That is strictly better than `Block(arbitrary AST)`: the
optimizer learns "asm clobbering X/Y" instead of "opaque — bail."

## Acceptance gate: byte-efficiency (not byte-identity)

The byte-exact gate of Plans 1–4 is replaced by a **byte-efficiency** gate:

- Compile every userland `.c` before and after; compare **per-function emitted
  byte size**. **New size ≤ old size for every function.**
- A size **increase** is allowed **only** when it is a clear performance win
  (e.g. it enables a `rep stos`/`rep movs`, or removes a memory round-trip) —
  and must be called out with that justification.
- Size **decreases are welcome and expected**: once the optimizer sees
  accesses uniformly, it can CSE/hoist repeated address math (this is part of
  (b)'s payoff).
- Correctness: `tests/test_asm.py` 49/49, `tests/test_programs.py` bbfs + ext2
  (incl. `e2fsck`), full unit suite, and especially the **rep-string suite**
  (the loop-idiom recognizer is the riskiest pass to re-port).

This relaxation is what makes (b) tractable: we no longer need bit-for-bit
reproduction of the old addressing; we need to not regress size.

## IR shape (forced by the byte-efficiency gate)

The naive LLVM-pure model — materialize a pointer with a GEP-like op, then
`load`/`store` through it — would emit `lea ebx,[…]; mov eax,[ebx]` where we
currently emit a single `mov eax,[_g_arr+12+ebx]`. That **bloats** with no perf
gain, so the gate rules it out (absent a full addressing-mode-selection pass,
which we are not building).

Therefore the new access ops **carry the symbolic address** — the
`_resolve_place` `(const_base, offset, index, scale)` form — as an SSA-visible
value, and **emission folds** an `Address` + `Load`/`Store` into a single x86
addressed instruction. The optimizer sees and can CSE/hoist the `Address`
values; emission keeps the encoding tight. This is the GCC "structured ref with
`get_inner_reference`" model adapted to cc.py's x86 addressing.

## Staged program (each stage = its own plan + PR, each gated)

> The recursion goal is explicit (see "Plan 5 goal" below) and lands in Stage 3.

- **Stage 1 — Foundation.** Build the **per-function byte-size differential
  harness** (the new gate tool). Define the uniform access IR ops: an `Address`
  value op carrying the symbolic `(const_base, offset, index, scale)` triple,
  and `Load(addr,width)` / `Store(addr,width,value)` / `AddressOf(addr)` /
  call-through-address ops. Lower the **currently-`Block`-emitted Place access
  family** (member / deref / subscript load·store·address-of·inc-dec·call) into
  them; emission folds them into tight x86 addressing. Optimizer passes updated
  only enough to treat the new ops safely (conservative). Gate: byte-size ≤
  baseline. `Index`/`IndexAssign` stay as-is this stage.

- **Stage 2 — Optimizer port.** Teach every pass to understand the new ops:
  copy-prop / const-fold operand rewriting, SSA value numbering over `Address`,
  LICM hoistability + memory-write detection, strength reduction, and — the
  highest-risk item — **rewrite the rep-string / loop-idiom matcher** to match
  the uniform `Load`/`Store`/`Address` shapes instead of `ir.Index`/
  `ir.IndexAssign`. This is where size *wins* appear (hoisted/CSE'd address
  math). Gate: byte-size ≤ baseline (expect improvements); rep-string suite
  green.

- **Stage 3 — Fold `Index`/`IndexAssign` + recursion (the old "Plan 5").**
  Parser emits `PlaceLoad`/`PlaceStore` over `SubscriptPlace(VariablePlace)` for
  `a[i]`; these lower into the Stage-1 uniform ops. **Make `_resolve_place`
  uniformly recursive** over `SubscriptPlace`/`MemberPlace`/`DereferencePlace`
  at any depth, retiring the 2-level `_emit_double_index_place_*` hack. Delete
  the `Index`/`IndexAssign` AST nodes (the IR ops are gone too, replaced by the
  uniform ops). Acceptance includes a **triple-index test** (`a[i][j][k]`) and
  chained `a->b[1][2]` / `(*p)[i][j]`.

- **Stage 4 — Retire `Block` entirely.** Give the remaining `Block` residents
  first-class ops: `VarDecl` / `ArrayDecl`, struct initializers, bitfield
  assigns, the long-type / const-chain / self-modify `Assign` special-cases,
  and route the `Call(name="asm")` + `ExtendedAsm` forms to opaque
  `InlineAsm`/`ExtendedAsm` ops. **Delete the `Block` node.** Gate: byte-size ≤
  baseline; full suite; `git grep "Block" cc/ir.py` → only history/none.

## Plan 5 goal (explicit): recursive, arbitrary-depth access

Folding `Index` is not just "handle `a[i]`": the stated, tested goal is
**uniform recursion** — `_resolve_place` resolves a `SubscriptPlace` /
`MemberPlace` / `DereferencePlace` of any depth by resolving its base
recursively, dereferencing if the base is a pointer, then applying the
subscript/member/deref. Arbitrary-depth `a[i][j][k]`, chained `a->b[1][2]`,
`(*p)[i][j]`, `s.grid[i][j].f[k]` must compile and run correctly (acceptance
tests), with the bespoke `_emit_double_index_place_*` 2-level helpers deleted.
(Optimization of deep accesses follows from Stage 2's optimizer port — the
inner contiguous dimension becomes a uniform `Load`/`Store` the rep-string pass
can match once LICM hoists the outer address.)

## Risk register

1. **Rep-string matcher rewrite (Stage 2)** — highest risk. If the uniform-op
   matcher misses a shape it previously caught, loops silently stop being
   `rep`-ified (size regression, caught by the gate); if it mis-identifies the
   induction step, miscompile (the store-index-miscompile class — caught by the
   rep-string suite + runtime tests). Port with the existing rep-string tests as
   the oracle and add uniform-op-shape tests.
2. **Addressing-mode folding (Stage 1)** — if emission fails to fold an
   `Address`+`Load` into one instruction, size regresses. The byte-size gate
   catches it per function.
3. **SSA over `Address` values** — `Address` ops that depend on a mutated index
   must not be CSE'd across the mutation; reuse the `_iter_ast_var_names` /
   induction-variable discipline from the store-index-miscompile fix.
4. **Scope creep of Stage 4** — the non-access residents (struct-init, the
   `Assign` special-cases) are fiddly for little optimizer payoff; keep their
   new ops thin and behavior-preserving.
