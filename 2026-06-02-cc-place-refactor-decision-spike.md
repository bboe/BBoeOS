# cc.py `Place` Refactor — Decision Spike: IR-vs-direct-emission fork (gates Plan 5)

**Status:** Decided — **option (a): keep the small IR; make `_resolve_place` the
shared address core.** This doc records the choice and rationale so Plan 5 (the
`Index` / `IndexAssign` fold) can be written.

**Context:** Plans 1–4 (PRs #573, #575, #576, #577) folded the member, deref,
`DoubleIndex`, address-of, increment/decrement, and indexed-call families onto
the recursive `Place` AST. The only access nodes left outside `Place` are
`Index` / `IndexAssign` (subscript on a *named* array, `a[i]`), deliberately
deferred because — unlike every other access shape — they are lowered into
**real IR ops** (`ir.Index` / `ir.IndexAssign`) that the SSA pass and the
loop / rep-string optimizer consume, rather than riding the `ir.Block` escape
hatch. Plan 5 folds them in. Before writing it we must pick how.

## The fork

- **(a) Keep the small IR.** Fold `Index`/`IndexAssign` at the **AST/parser
  level** (parser emits `PlaceLoad(SubscriptPlace(VariablePlace(a), i))` /
  `PlaceStore(...)`), and teach the **IR builder** to recognize that one Place
  shape and lower it to the *existing* `ir.Index` / `ir.IndexAssign` ops. The
  SSA pass, copy-prop, LICM, strength reduction, and the rep-string recognizer
  stay **completely untouched** — they still see `ir.Index` / `ir.IndexAssign`.
  `_resolve_place` remains the shared address-computation core for the
  `Block`-emitted Place shapes (member / deref / double-index), exactly as
  Plans 1–4 left it. Delete the `Index` / `IndexAssign` **AST** nodes; keep the
  identically-named **IR** ops.

- **(b) Grow the IR.** Lower *all* `Place` into new IR address/load/store ops,
  retire `Block` for accesses, and re-express the optimizer over IR Place ops —
  gaining optimization on member access "for free."

## Findings that drove the decision (recon over cc/ir.py, ssa.py, ir_optimize.py, loops.py, codegen)

1. **The rep-string / loop optimizer is hard-coupled to the `ir.Index` /
   `ir.IndexAssign` op shapes.** `cc/loops.py` matches the exact pair
   `ir.Index(base=S, index=IV)` + `ir.IndexAssign(base=D, index=IV, source=t)`
   with the induction variable as a **bare operand** (`isinstance(load,
   ir.Index)` / `isinstance(store, ir.IndexAssign)`, and `store.index != IV`
   guards). Under (b), the IV would sit several levels deep inside a
   `SubscriptPlace(VariablePlace(...))` IR op and this matcher — the one PR #566
   just added — would need a full rewrite. Under (a) it is **untouched**.

2. **Multiple passes already reach into `Index`/`IndexAssign` operands.**
   Copy-propagation and constant folding in `ir_optimize.py` actively rewrite
   the `index` / `source` operands (`_substitute_value`); `_compute_use_counts`
   counts the bases as reads; LICM (`loops.py`) has `Index`-specific
   memory-safety / hoistability logic (`_is_hoistable_kind` includes
   `ir.Index`); SSA explicitly excludes `Index`/`IndexAssign` bases from
   renaming. Option (b) means **every one of these passes must learn to walk
   `Place` trees** instead of flat IR ops — a large, error-prone surface with
   real miscompile risk (the store-index-miscompile class, PR #568, lives
   exactly here). Option (a) leaves all of them alone.

3. **The two paths share nothing today, and (a) needs them to share little.**
   `_resolve_place` is currently called *only* from the `Block`-emitted Place
   path — never from the IR `Index` lowering. The IR `Index`/`IndexAssign` ops
   have their own emit path. Under (a), the named-array subscript continues to
   lower to `ir.Index`/`ir.IndexAssign` (its own emit path, optimizer-visible),
   and the *other* Place shapes keep using `_resolve_place` — so no new
   cross-path plumbing is forced, and the duplication that does exist is
   acceptable and documented.

4. **The "small IR" stance is embedded throughout.** `ir.py` lowers only the
   straightforward shapes (arithmetic, simple assigns, control flow, named-array
   subscript) and routes everything complex through `Block` ("Escape hatch:
   lower this AST node via the existing statement codegen"). `ir_optimize.py`
   states it does not rewrite `Block` nodes. There are 15 IR op types; (b) adds
   a Place-address/load/store family on top and forces it through every pass.
   (b) runs directly against this stance.

5. **(b) throws away Plans 1–4's investment.** The `_resolve_place` /
   `_emit_place_*` / `_emit_member_*` / `_emit_dereference_place_*` /
   `_emit_double_index_place_*` family (~700+ lines, byte-exact, fully tested)
   becomes interim scaffolding under (b). Under (a) it is the **permanent**
   shared core.

## Decision: (a)

(a) is lower risk, preserves the byte-exact `_resolve_place` core, leaves the
SSA + loop/rep-string + copy-prop + LICM passes (and their golden/byte-exact
behavior) untouched, and matches the project's deliberate small-IR design. The
one facet (b) would improve — optimization on member access — is not currently
needed (member access is not in loops that the rep-string/LICM passes target),
and can be revisited later without re-deciding this fork. The dual path
(IR for named-array subscript, `Block` + `_resolve_place` for everything else)
stays as a **deliberate, documented** design point rather than a smell to
eliminate.

## What Plan 5 looks like under (a)

The fold becomes primarily an **AST-level unification with an IR-builder
recognizer**, byte-exact like Plans 1–4:

1. **Parser:** emit `PlaceLoad(SubscriptPlace(VariablePlace(a), i))` for `a[i]`
   reads and `PlaceStore(SubscriptPlace(VariablePlace(a), i), v)` for `a[i] = v`
   (and the compound / `+=` forms), replacing the `Index` / `IndexAssign` AST
   constructions. The `DoubleIndex`-style `a[i][j]` already composes
   (`SubscriptPlace(DereferencePlace(Index(...)), j)`) — Plan 5 lets the inner
   `a[i]` also be a `SubscriptPlace`, so the bespoke `_emit_double_index_place_*`
   helpers can fold into genuine recursion (and `a[i][j][k]` falls out for free).

2. **IR builder (`cc/ir.py`):** add a recognizer that maps the named-array
   subscript Place shape — `PlaceLoad`/`PlaceStore` over
   `SubscriptPlace(VariablePlace, index)` — back to the existing `ir.Index` /
   `ir.IndexAssign` ops, so SSA + every optimizer pass + the rep-string
   recognizer keep operating on the identical IR they see today. This is the
   single load-bearing change and the byte-exact gate's focus.

3. **`_resolve_place`:** extend to resolve `SubscriptPlace(VariablePlace, index)`
   for the `Block`-emitted contexts that don't go through the IR (e.g. a bare
   `a[i]` sub-expression the IR builder leaves to `Block`), reusing the existing
   array-base address math. Becomes the single shared address core for all
   non-IR Place shapes.

4. **Delete** the `Index` / `IndexAssign` **AST** node classes (the IR ops of
   the same name stay). Migrate any remaining AST-`Index` consumers (e.g. the
   `_emit_double_index_place_*` inner-`Index`, `_expression_type`, liveness) to
   the Place shape.

**Gate (same discipline as Plans 1–4):** the `tests/test_cc_place.py` golden
stays byte-identical for all subscript shapes captured from pre-Plan-5 output;
the userland differential (all 50 `.c`) stays byte-identical; `test_asm` 49/49;
`test_programs` bbfs + ext2 green. The rep-string suite (`rep_loops` tests) is
the critical regression check — if the IR-builder recognizer is correct, those
stay green unchanged.

**Risk note:** the highest-risk item is step 2 (the IR-builder recognizer) — if
the named-array subscript Place shape isn't mapped to `ir.Index`/`ir.IndexAssign`
exactly, the loop/rep-string optimizer silently stops firing (no error, just
worse code) or, worse, the induction-variable walkers miss a step (the
store-index-miscompile class). The userland differential + rep-string suite are
the gates that catch this; Plan 5 must run both before declaring done.
