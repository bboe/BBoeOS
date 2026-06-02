# cc.py Place Refactor — Plan 5 / Stage 2 Design: structured operands for ir.Access

**Status:** Designed (brainstormed + approved). Supersedes the decision-spike's
original "Stage 2 = optimizer port (rewrite the rep-string matcher)" framing —
see "Resequencing" below.

**Goal:** Give `ir.Access` first-class IR operands by lowering every
value-bearing sub-expression of a migrated Place to an IR temp, and teach the
*linear* optimizer passes (operand enumeration, copy-propagation, use-counts,
dead-code elimination) to see through `ir.Access`. Byte-efficiency gate: every
function ≤ baseline; size *wins* (dead pure-load removal) welcome.

## Resequencing (why this is Stage 2, not the decision-doc's Stage 2)

The decision spike put "rewrite the rep-string / loop-idiom matcher" in Stage 2.
Recon during Stage-1 close-out showed that matcher (`_match_copy_body` /
`_match_fill_body` in `cc/loops.py`) hard-matches `ir.Index` / `ir.IndexAssign`
— the *named-array* subscript ops, which Stage 1 deliberately did **not**
migrate and which only change shape in the **Stage 3 Index fold**. The
member / dereference / subscript-of-expression accesses that Stage 1 moved onto
`ir.Access` are not what the rep-string / LICM passes target. So the matcher
rewrite is inseparable from the Index fold and belongs in Stage 3.

Stage 2 is therefore the *true intermediate*: grow `ir.Access` from an opaque
AST wrapper into an op with first-class operands, and teach the linear passes to
optimize it. This is the plumbing Stage 3 exploits when `ir.Index` folds into
the same ops and the rep-string / LICM / SSA work (Approach 2 below) lands where
it actually pays off — in one coherent pass over the loop machinery.

## Scope decision: full operand lowering, linear passes only (Approach 1)

Two forks were resolved during the brainstorm:

1. **Operand model = full operand lowering.** Every value-bearing leaf of a
   migrated Place is lowered to an IR temp; the Place AST leaf becomes
   `Var(temp)` (or stays an `Int`/`String` literal). Not "reads-only" and not
   "lower only simple operands" — the full representation, so the optimizer sees
   the complete operand set.

2. **Pass scope = linear passes only (Approach 1).** Operand enumeration +
   copy-prop substitution + use-counts + dead-pure-load DCE. SSA-over-operands,
   LICM hoisting, and CSE of address math (Approach 2) are **deferred to
   Stage 3**, where hot loops over accesses first exist (post Index fold) and
   where the SSA change can be done alongside the rep-string / induction-variable
   work it overlaps.

With full operand lowering, **copy-propagation into `ir.Access` operands is
mandatory, not optional**: simple indices lower to `Copy` temps (`t = i`), and
copy-prop folding them back to `i` is what keeps emitted code byte-neutral.

### Approach 1 → Approach 2 is additive, not rework

Stage 2 builds the load-bearing infrastructure Approach 2 reuses unchanged:
operand **lowering**, **enumeration**, **substitution**, operand-based
**use-counts**, and pure-load **classification**. Approach 2 (Stage 3) then:
removes `ir.Access` from `_opaque_referenced_names` (one line) and wires rename
into operand positions (rename *is* substitution); removes `ir.Access` from the
three `cc/loops.py` opaque-barrier tuples (one line each) and adds a
hoistability predicate (consumes enumeration); value-numbers operands for CSE.
Nothing in Stage 2 is thrown away. The single design obligation that makes this
clean: build enumeration/substitution as **reusable module-level primitives**,
not logic private to copy-prop.

## Architecture

### The representation change (Builder, cc/ir.py)

Before wrapping a migrated access (`PlaceLoad` / `PlaceStore` / `PlaceCall`, in
statement or expression position), the `Builder` lowers every value-bearing leaf
of the Place to an IR temp via the existing `_build_expr`, then rewrites the leaf
to reference it. The value-bearing leaf positions are exactly:

- `SubscriptPlace.index` (a `Node`)
- `DereferencePlace.pointer` (a `Node`)
- `PlaceStore.value` (a `Node`)
- each element of `PlaceCall.args` (a `list[Node]`)

`MemberPlace.base` / `VariablePlace.name` carry only names — nothing to lower;
the recursion descends through `MemberPlace.base` and `SubscriptPlace.base` to
reach nested leaves. The expression-position wrapper
`Assign(expr=<PlaceLoad/PlaceCall>, name=temp)` is handled by descending through
the `Assign` to the Place.

Worked example — `points[i].path[j + 1] = v`:

```
t0 = i             # Copy   — copy-prop folds back to i (byte-neutral)
t1 = j + 1         # BinaryOperation — one use, stays
t2 = v             # Copy   — folds back to v (byte-neutral)
Access(node = PlaceStore(
                SubscriptPlace(
                  MemberPlace(SubscriptPlace(VariablePlace("points"), Var(t0)), "path"),
                  Var(t1)),
                Var(t2)))
```

Genuinely-complex leaves (`t1`) were already evaluated to a scratch value before
the access in today's codegen — the IR merely names the result. Simple leaves
(`t0`, `t2`) are `Copy`s that copy-prop collapses, so emitted bytes match today.

### Reusable operand primitives (cc/ir.py, module-level)

The bridge between the optimizer's flat `ir.Value` (`int | str |
PlaceAddressOf`) world and the `ir.Access` AST payload:

- `iter_access_operands(node) -> Iterator[ir.Value]` — walk the `ir.Access` AST
  (through the optional `Assign` wrapper, recursively through the Place tree),
  yielding each operand leaf as an `ir.Value` in a stable left-to-right order:
  `Var(name) → name` (str), `Int(value) → value` (int), `PlaceAddressOf` passed
  through, `String` label → its name. Non-`Value` leaves (none expected after
  lowering, but defensive) are skipped.
- `substitute_access_operand(node, *, source, target) -> Node` — return a new
  AST (frozen-dataclass `replace` down the path to each match) with every
  operand leaf equal to `target` rewritten to `source`, mapping the `ir.Value`
  back to its AST-leaf form (`name → Var(name)`, `int → Int(value)`,
  `PlaceAddressOf` as-is).

These are public (consumed by `ir_optimize` now, and by `ssa` / `loops` in
Stage 3). They must round-trip: substituting `target→target` is identity, and
enumeration after substitution reflects the substitution.

### Optimizer pass changes (cc/ir_optimize.py — linear passes)

- `_instruction_value_operands`: add an `ir.Access` arm returning
  `tuple(iter_access_operands(instruction.node))`.
- `_substitute_value`: add an `ir.Access` arm returning
  `dataclasses.replace(instruction, node=substitute_access_operand(instruction.node, source=source, target=target))`
  (only when `target` is actually among the operands — cheap guard via
  enumeration, matching the other arms' early-return style).
- `_compute_use_counts`: the `ir.Access` branch switches from the conservative
  `_iter_ast_var_names(instruction.node)` whole-tree walk to counting the
  operands from `_instruction_value_operands` (so a name used only as an access
  operand is counted exactly per occurrence, and its defining temp becomes
  DCE-eligible when otherwise dead).
- `_has_side_effects`: refine the `ir.Access` case — an access wrapping a
  `PlaceStore` or `PlaceCall` is side-effecting (memory write / call) and kept;
  an access wrapping a pure `PlaceLoad` (the `Assign(expr=PlaceLoad, name=temp)`
  expression-position form) is **not** inherently side-effecting, so DCE may
  drop it when its destination temp is dead. This dead-pure-load removal is the
  one new size win this stage unlocks (reported as IMPROVED by the gate).

### What stays conservative (the Approach-2 seam, unchanged from Stage 1)

- `cc/ssa.py` `_opaque_referenced_names` keeps `ir.Access` in the opaque set —
  access operands stay excluded from SSA renaming.
- `cc/loops.py` keeps `ir.Access` in the three opaque-barrier tuples
  (`_iter_read_names`'s consumer, `_iv_address_taken_in_loop`,
  `_names_defined_in_loop`).
- `ir.Index` / `ir.IndexAssign` and the rep-string matcher are untouched.

### Codegen impact (cc/codegen/x86)

`_resolve_place` and the place codegen already evaluate operand positions via
`generate_expression(...)` on `place.index` / `DereferencePlace.pointer` /
`PlaceStore.value` / call args. After lowering those positions hold `Var(temp)`,
so `generate_expression(Var(temp))` emits a load from the temp's slot/register;
the existing slot-coalescing + auto-pin machinery keeps simple operands in
registers, which is why copy-prop'd simple operands stay byte-identical. **No
`_resolve_place` change is expected.** If a complex single-use operand class
shows a store/load round-trip regression, the per-function size gate catches it
and the mitigation is to leave that class inline (not lower it) rather than
accept the regression.

## Data flow

`Builder._build_stmt` / `_build_expr` (lowering) → `ir.Access` with operand-leaf
`Var`s → `Optimizer` (copy-prop folds simple temps; DCE drops dead pure loads)
→ emission `generate_statement(node)` (unchanged dispatch) → `_resolve_place` /
place codegen reading the (now `Var`/literal) operand positions.

## Testing

- **Byte-size gate** (`tests/test_cc_function_sizes.py`): every function ≤
  baseline; IMPROVED welcome (dead-pure-load DCE). The authority.
- **`cc_place` golden** (`tests/test_cc_place.py`): the golden **will change**
  this stage — operand lowering reshapes the IR. Regenerate only where the
  emission is provably equal-or-smaller, and explain every delta in the PR; no
  delta may be a size increase.
- **New unit tests** (`tests/unit/`): the two primitives over representative
  shapes (`*p`, `b->n`, `arr[i].f[j+1]`, `fp[i](x)`) — round-trip, stable order,
  `Assign`-wrapper + arbitrary-depth handling; copy-prop folding of simple
  operands; dead-pure-load DCE; verification that side-effecting accesses
  (`PlaceStore` / `PlaceCall`) are never DCE'd.
- **Full matrix** (run-everything rule for IR/codegen changes): `test_asm`,
  `test_programs` bbfs + ext2, `test_bboefs`, `tests/unit/`.

## Risk register

1. **Byte regression on complex single-use operands** — the round-trip risk.
   Gate-caught per function; mitigation is to leave the offending operand class
   inline rather than lower it. Likely to surface for `DereferencePlace.pointer`
   expressions (`*(p + off)`) and `j + 1`-style subscripts.
2. **Golden churn** — the `cc_place` golden necessarily shifts. Discipline:
   explain every delta; none may increase a function's size.
3. **Primitive correctness** — `iter_access_operands` /
   `substitute_access_operand` must handle the `Assign` wrapper, arbitrary Place
   depth, and the `ir.Value`↔AST-leaf mapping (incl. `PlaceAddressOf`). Covered
   by dedicated unit tests; a bug here is a miscompile, so the unit tests are
   the gate, not an afterthought.
4. **Copy-prop interaction with multi-def temps** — copy-prop already excludes
   multi-def temps from propagation; the `ir.Access` substitution arm inherits
   that guard by going through the same `_substitute_value` / propagation
   driver. No new propagation policy is introduced.
