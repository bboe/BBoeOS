# cc.py Register Allocator PR 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `cc.regalloc.color()` the single authority for the register homes of user locals and parameters, deleting the hand-tuned auto-pin heuristic, with no per-function `.text` byte regression.

**Architecture:** Allocation is orthogonal to the emission path. The existing AST `LivenessAnalyzer.interference()` (which works for every function, including `main`) feeds `cc.regalloc.color()` directly (no `ir.Function` needed). The retained auto-pin economics (reference counts, per-register clobber save costs, candidate eligibility, BP-as-index-cost) become the engine's `CostModel`/`RegisterConstraints` inputs. `Allocation.homes` populates the existing `self.pinned_register` map; spilled locals/params take frame slots via the existing `allocate_local` path. Emission (AST or IR) is untouched.

**Tech Stack:** Python 3.12, the cc.py compiler (`cc/` package), NASM, `pytest` for `tests/unit/`, standalone Python test scripts for the byte gate and golden parity.

**Worktree:** `/home/ubuntu/bboeos/.claude/worktrees/regalloc-pr2` on branch `bboe/cc-regalloc-pr2` (already created off `origin/main`). Run all commands there.

**Reference (verbatim current code), confirmed in the worktree:**
- `cc/codegen/x86/generator.py`: `AutoPinTallyState` (lines 95–113); `_select_auto_pin_candidates` (4540–4729); `compute_safe_pin_registers` (5533–5614, assigns `self.register_clobber_counts` at 5603); `can_auto_pin` (5509–5531); `allocate_local` (5482–5502); `scan_locals` reads `auto_pin_candidates` at 6319–6326; `BUILTIN_CLOBBERS` (178–244).
- `cc/codegen/x86/emission.py`: `generate_function` (3176–3534) — auto-pin init at 3195–3196, `safe_pin_registers` at 3338, `_select_auto_pin_candidates` call at 3353–3355, `in_register` param setup at 3357–3380, `scan_locals` call at 3381, dual-path emission at 3481–3498; emission routing at 2208–2250 (`main`/`naked`/`always_inline`/prototype stay on AST emission).
- `cc/regalloc.py`: `color(*, constraints, costs, interference, moves) -> Allocation` (374–443); `Allocation(homes: dict[str,str], spilled: frozenset[str])`; `CostModel(register_save_cost: dict[str,dict[str,int]], spill_benefit: dict[str,int])`; `RegisterConstraints(allowed: dict[str,frozenset[str]], pool: tuple[str,...], precolored: dict[str,str])`. `color()` picks `min(legal, key=lambda r: (save_costs.get(r,0), pool.index(r)))` and spills when `name in spill_benefit and benefit <= save_costs.get(choice,0)`.
- `cc/codegen/liveness.py`: `LivenessAnalyzer(*, body, parameters)`, `.interference() -> dict[str, set[str]]`, raises `LivenessAnalysisError` for unmodeled nodes (already imported in `generator.py`).
- `cc/target.py`: 16-bit `register_pool = ("dx","cx","bx","di")`, `base_register="bp"`; 32-bit `register_pool=("edx","ecx","ebx","edi")`, `base_register="ebp"`; `LOW_BYTE` (lines 33–50) lists registers with an 8-bit alias — `di`/`edi`/`si`/`bp` are **absent** (no byte alias).
- `cc/ast_nodes.py`: `Param.in_register: str | None`, `Param.type: str`, `Param.is_array: bool`; `Function.regparm_count`, `Function.naked`.

**Test invocation (from the worktree root):**
- Byte gate: `python3 tests/test_cc_function_sizes.py` (refresh baseline: `BBOE_UPDATE_SIZES=1 python3 tests/test_cc_function_sizes.py`).
- Unit: `python3 -m pytest tests/unit/ -q`.
- 16/32-bit legality: `python3 tests/test_cc_bits.py`.
- Place golden: `python3 tests/test_cc_place.py` (re-bless: `BBOE_UPDATE_GOLDEN=1 python3 tests/test_cc_place.py`).
- Self-host + runtime: `python3 tests/test_asm.py`, `python3 tests/test_programs.py --slow`, `python3 tests/test_programs.py --filesystem ext2 --slow`, `python3 tests/test_bboefs.py`.

**Commit-message footer (every commit):**
```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## File Structure

- **Modify** `cc/codegen/x86/generator.py` — add an `AutoPinEconomics` dataclass + a `_compute_pin_economics` method (Task 1); add `_allocator_homes` (Task 4); delete `_select_auto_pin_candidates` + tally helpers + `AutoPinTallyState` (Task 6).
- **Create** `cc/codegen/x86/regalloc_inputs.py` — the pure adapter: economics + target model → `allocatable` / `CostModel` / `RegisterConstraints` / `interference` (Task 3). Lives next to the generator; one responsibility (translate cc economics into engine inputs).
- **Modify** `cc/codegen/x86/emission.py` — snapshot homes into `self.register_homes` (Task 2); call the allocator behind a flag and map its result (Task 4); flip the default and remove the heuristic call (Task 6).
- **Create** `tests/unit/test_cc_regalloc_inputs.py` — unit tests for the adapter (Task 3).
- **Create** `tests/test_cc_register_homes.py` — golden parity test over the corpus (Task 2).
- **Create** `tests/golden/cc_register_homes_baseline.json` — frozen `{source: {function: {var: register}}}` snapshot (Task 2, regenerated in Task 6 once the allocator is authoritative).

---

## Task 1: Extract auto-pin economics into a pure, reusable method (behavior-preserving)

Pull the front half of `_select_auto_pin_candidates` (everything that *gathers* economics, before ranking/assignment) into a method returning a structured bundle. The heuristic keeps working identically; the adapter (Task 3) will reuse the same bundle.

**Files:**
- Modify: `cc/codegen/x86/generator.py` (add `AutoPinEconomics` near `AutoPinTallyState` at line 95; add `_compute_pin_economics`; refactor `_select_auto_pin_candidates` to call it)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_cc_pin_economics.py`:

```python
"""The extracted economics bundle reproduces the inputs auto-pin consumes."""

from cc.ast_nodes import Assign, Int, Param, Var, VarDecl
from cc.codegen.x86.generator import X86CodeGenerator


def _generator() -> X86CodeGenerator:
    return X86CodeGenerator(bits=32, target_mode="user")


def test_reference_counts_and_allocatable_for_simple_body() -> None:
    # int total = 0; total = total + 1;  -> 'total' referenced, eligible, no index use.
    body = [
        VarDecl(name="total", type_name="int", init=Int(value=0), line=1),
        Assign(target=Var(name="total", line=2), value=Int(value=1), line=2),
    ]
    generator = _generator()
    generator.safe_pin_registers = generator.compute_safe_pin_registers(body, parameters=[])
    economics = generator._compute_pin_economics(body=body, parameters=[])
    assert "total" in economics.allocatable
    assert economics.reference_counts.get("total", 0) >= 2
    assert economics.index_uses.get("total", 0) == 0
    assert "total" not in economics.address_taken


def test_address_taken_local_is_excluded_from_allocatable() -> None:
    from cc.ast_nodes import AddressOf

    body = [
        VarDecl(name="slot", type_name="int", init=Int(value=0), line=1),
        VarDecl(name="ptr", type_name="int", init=AddressOf(operand=Var(name="slot", line=2), line=2), line=2),
    ]
    generator = _generator()
    generator.safe_pin_registers = generator.compute_safe_pin_registers(body, parameters=[])
    economics = generator._compute_pin_economics(body=body, parameters=[])
    assert "slot" not in economics.allocatable
    assert "slot" in economics.address_taken
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_pin_economics.py -q`
Expected: FAIL — `AttributeError: 'X86CodeGenerator' object has no attribute '_compute_pin_economics'`.

- [ ] **Step 3: Add the `AutoPinEconomics` dataclass**

In `cc/codegen/x86/generator.py`, immediately after the `AutoPinTallyState` dataclass (after line 113), add:

```python
@dataclass(kw_only=True, slots=True)
class AutoPinEconomics:
    """The register-allocation economics gathered from a function body.

    The pure inputs both the legacy auto-pin heuristic and the regalloc
    adapter consume: which locals/params are pin-eligible, how often each is
    referenced (the spill benefit), how many subscript uses each has (the BP
    index penalty), and which pre-first-store call clobbers are elided per
    candidate/register.  ``byte_typed`` is the subset whose width has no 8-bit
    register alias outside AL/BL/CL/DL (so they may not be homed in DI/SI/BP).
    """

    address_taken: set[str] = field(default_factory=set)
    allocatable: frozenset[str] = field(default_factory=frozenset)
    byte_typed: frozenset[str] = field(default_factory=frozenset)
    index_uses: dict[str, int] = field(default_factory=dict)
    pre_store_clobbers: dict[str, dict[str, int]] = field(default_factory=dict)
    reference_counts: dict[str, int] = field(default_factory=dict)
```

- [ ] **Step 4: Add `_compute_pin_economics` by lifting the gathering prefix**

Add this method to `X86CodeGenerator` (place it immediately before `_select_auto_pin_candidates`, ~line 4540). The body is the verbatim gathering prefix of `_select_auto_pin_candidates` (everything from the `param_candidates`/`body_candidates` collection through `pre_store_clobbers`, plus the expression-temporary and address-taken filters that produce the final eligible set), repackaged to return the bundle. `byte_typed` is computed by collecting `VarDecl.type_name`/`Param.type` in the byte-width set.

```python
def _compute_pin_economics(self, *, body: list[Node], parameters: list, apply_liveness_elision: bool = True) -> AutoPinEconomics:
    """Gather the pin economics for *body* (pure; no register assignment).

    Mirrors the candidate-collection and tally prefix of
    :meth:`_select_auto_pin_candidates`: eligible candidates (params + body
    locals minus asm-operand, expression-temporary, and address-taken vars),
    reference counts, subscript counts, and per-candidate pre-first-store
    clobbers.  Both the legacy heuristic and the regalloc adapter consume the
    returned bundle.
    """
    self.switch_pin_overrides = set()

    param_candidates: list[tuple[str, int]] = []
    byte_types = {"char", "signed char", "unsigned char", "_Bool"}
    byte_typed: set[str] = set()
    for order, param in enumerate(parameters):
        if param.is_array:
            continue
        param_candidates.append((param.name, order))
        if param.type in byte_types:
            byte_typed.add(param.name)

    body_candidates: list[tuple[str, int]] = []
    function_pointer_vars: set[str] = self._collect_function_pointer_vars(body, parameters=parameters)
    self._collect_auto_pin_body_candidates(body, body_candidates=body_candidates, top_level=True)
    asm_operand_vars = self._collect_asm_operand_vars(body)
    body_candidates = [(name, o) for name, o in body_candidates if name not in asm_operand_vars]

    state = AutoPinTallyState(body_candidates=body_candidates)
    for statement in body:
        self._tally_auto_pin_counts(statement, state=state)
    address_taken = state.address_taken
    ax_resident_uses = state.ax_resident_uses
    body_candidates = state.body_candidates
    counts = state.counts
    index_uses = state.index_uses
    init_count = state.init_count
    init_expr = state.init_expr
    other_uses = state.other_uses

    candidate_names = {name for name, _ in body_candidates}
    pre_store_clobbers: dict[str, dict[str, int]] = {name: {} for name in candidate_names}
    written: dict[str, bool] = dict.fromkeys(candidate_names, False)
    for statement in body:
        self._tally_pre_store_clobbers(
            statement,
            candidate_names=candidate_names,
            function_pointer_vars=function_pointer_vars,
            pre_store_clobbers=pre_store_clobbers,
            written=written,
        )

    self._collect_byte_typed_locals(body, byte_types=byte_types, byte_typed=byte_typed)

    eligible: list[tuple[str, int]] = body_candidates + param_candidates
    eligible = [
        item
        for item in eligible
        if not self._is_candidate_expression_temporary(
            item[0],
            ax_resident_uses=ax_resident_uses,
            init_count=init_count,
            init_expr=init_expr,
            other_uses=other_uses,
        )
    ]
    eligible = [item for item in eligible if item[0] not in address_taken]

    return AutoPinEconomics(
        address_taken=address_taken,
        allocatable=frozenset(name for name, _ in eligible),
        byte_typed=frozenset(byte_typed),
        index_uses=index_uses,
        pre_store_clobbers=pre_store_clobbers,
        reference_counts=counts,
    )
```

Add the small byte-typed collector helper near the other `_collect_*` helpers:

```python
def _collect_byte_typed_locals(self, statements: list[Node], /, *, byte_types: set[str], byte_typed: set[str]) -> None:
    """Record names of VarDecl locals whose declared type has no high-register byte alias."""
    for statement in statements:
        if isinstance(statement, VarDecl) and statement.type_name in byte_types:
            byte_typed.add(statement.name)
        for slot in getattr(type(statement), "__slots__", ()):
            child = getattr(statement, slot, None)
            if isinstance(child, list):
                self._collect_byte_typed_locals([item for item in child if isinstance(item, Node)], byte_types=byte_types, byte_typed=byte_typed)
```

- [ ] **Step 5: Refactor `_select_auto_pin_candidates` to consume the bundle**

Replace the gathering prefix of `_select_auto_pin_candidates` (the code from `self.switch_pin_overrides = set()` through the two filter list-comprehensions that build the filtered `combined`) with:

```python
if not self.safe_pin_registers:
    return {}
economics = self._compute_pin_economics(body=body, parameters=parameters, apply_liveness_elision=apply_liveness_elision)
counts = economics.reference_counts
index_uses = economics.index_uses
pre_store_clobbers = economics.pre_store_clobbers
allocatable = economics.allocatable
# Rank the eligible candidates by reference count, declaration order tiebreak.
combined = [(name, order) for order, name in enumerate(sorted(allocatable, key=lambda n: (-counts.get(n, 0), n)))]
```

Keep the existing assignment loop (the `available`/`register_holders`/`deferred_for_sharing` logic and the sharing pass) **unchanged** below this point — it already reads `counts`, `index_uses`, `pre_store_clobbers`, `self.register_clobber_counts`, and `self.safe_pin_registers`.

> Note: the original ranking interleaved body-then-param order; reproduce the **exact** original ordering by ranking `body_candidates` then `param_candidates` separately if Step 7's byte gate shows any drift. Keep `AutoPinEconomics` carrying both lists only if needed — prefer the single sorted list first and confirm via the gate.

- [ ] **Step 6: Run the unit tests**

Run: `python3 -m pytest tests/unit/test_cc_pin_economics.py tests/unit/ -q`
Expected: PASS.

- [ ] **Step 7: Run the byte gate (behavior-preserving check)**

Run: `python3 tests/test_cc_function_sizes.py`
Expected: PASS with **no** size changes (this refactor must be byte-neutral). If any function changed, the ranking order drifted — restore the exact body-then-param ranking from the original (rank each list separately, concatenate) and re-run.

- [ ] **Step 8: Commit**

```bash
git add cc/codegen/x86/generator.py tests/unit/test_cc_pin_economics.py
git commit -m "$(cat <<'EOF'
refactor(cc): extract auto-pin economics into reusable AutoPinEconomics

Pull the candidate-collection + tally prefix of _select_auto_pin_candidates
into _compute_pin_economics returning a structured bundle. Byte-neutral;
the heuristic still drives homes. Prepares the regalloc adapter (PR 2).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Capture per-function register homes and freeze the golden

Snapshot the heuristic's `{var: register}` map per function during codegen, add a corpus-wide golden test, and commit the golden generated by the current (heuristic) compiler.

**Files:**
- Modify: `cc/codegen/x86/emission.py` (snapshot `self.pinned_register` at the end of `generate_function`)
- Modify: `cc/codegen/x86/generator.py` (`__init__`: initialize `self.register_homes`)
- Create: `tests/test_cc_register_homes.py`
- Create: `tests/golden/cc_register_homes_baseline.json`

- [ ] **Step 1: Initialize the homes accumulator**

In `X86CodeGenerator.__init__` (near line 359 where `self.pinned_register` is initialized), add:

```python
self.register_homes: dict[str, dict[str, str]] = {}  # function name -> {var: register}
```

- [ ] **Step 2: Snapshot homes after they are finalized**

In `generate_function` (`cc/codegen/x86/emission.py`), immediately after `self.scan_locals(body)` (line 3381), add:

```python
        # Record the final register-home map for this function so the golden
        # parity test (tests/test_cc_register_homes.py) can compare the
        # allocator's choices against the heuristic's.
        self.register_homes[function.name] = dict(self.pinned_register)
```

- [ ] **Step 3: Write the golden parity test**

Create `tests/test_cc_register_homes.py` (standalone script — follows `test_cc_function_sizes.py`'s shape, not pytest):

```python
#!/usr/bin/env python3
"""Per-function register-home parity gate.

Compiles every userland C source and compares each function's {var: register}
home map against tests/golden/cc_register_homes_baseline.json.  Refresh the
golden deliberately with BBOE_UPDATE_HOMES=1 only when an assignment change is
intended and byte-verified (tests/test_cc_function_sizes.py is the hard gate).
"""

import json
import os
import sys
from pathlib import Path

from cc.codegen.x86.emission import compile_source_homes  # added in Step 4

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "cc_register_homes_baseline.json"
SOURCES = sorted({*ROOT.glob("user/libbboeos/*.c"), *ROOT.glob("user/programs/*.c")})

# Documented byte-neutral register-identity exceptions: {source: {function: reason}}.
# A function listed here may differ from the golden as long as the byte gate stays green.
IDENTITY_EXCEPTIONS: dict[str, dict[str, str]] = {}


def current_homes() -> dict[str, dict[str, dict[str, str]]]:
    result: dict[str, dict[str, dict[str, str]]] = {}
    for source in SOURCES:
        rel = str(source.relative_to(ROOT))
        result[rel] = compile_source_homes(source=source)
    return result


def main() -> int:
    current = current_homes()
    if os.environ.get("BBOE_UPDATE_HOMES") == "1":
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"wrote {GOLDEN}")
        return 0
    baseline = json.loads(GOLDEN.read_text())
    failures: list[str] = []
    for source, functions in baseline.items():
        for name, homes in functions.items():
            got = current.get(source, {}).get(name)
            if got is None:
                failures.append(f"{source}:{name}: missing from current build")
            elif got != homes and name not in IDENTITY_EXCEPTIONS.get(source, {}):
                failures.append(f"{source}:{name}: homes {homes} -> {got}")
    if failures:
        print("REGISTER-HOME PARITY FAILURES:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"register-home parity OK ({len(SOURCES)} sources)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Add the `compile_source_homes` helper**

In `cc/codegen/x86/emission.py`, add a module-level function that compiles one source to assembly (object/32-bit, mirroring how `tests/test_cc_function_sizes.py` invokes the compiler) and returns `generator.register_homes`. Reuse the existing programmatic compile entry the size gate already uses — locate it with `grep -n "def function_sizes" tests/test_cc_function_sizes.py` and follow its compiler call; expose the same call returning the generator's `register_homes` attribute. Keep it side-effect-free (no file writes).

```python
def compile_source_homes(*, source) -> dict[str, dict[str, str]]:
    """Compile *source* (32-bit, object mode) and return per-function register homes."""
    from pathlib import Path  # local import keeps module import graph unchanged

    text = Path(source).read_text()
    generator = compile_text_to_generator(source=text, bits=32, target_mode="user")  # see note
    return dict(generator.register_homes)
```

> Implementation note: if no `compile_text_to_generator` seam exists yet, add a thin one beside the existing compile path so the generator object is reachable after `.generate(...)`. The size gate already constructs the generator internally; factor the minimal shared seam rather than duplicating the parse→lower→generate sequence.

- [ ] **Step 5: Generate and verify the golden**

Run: `BBOE_UPDATE_HOMES=1 python3 tests/test_cc_register_homes.py`
Then: `python3 tests/test_cc_register_homes.py`
Expected: second run prints `register-home parity OK`.

- [ ] **Step 6: Run the byte gate (unchanged)**

Run: `python3 tests/test_cc_function_sizes.py`
Expected: PASS, no changes (capture is observation-only).

- [ ] **Step 7: Commit**

```bash
git add cc/codegen/x86/emission.py cc/codegen/x86/generator.py tests/test_cc_register_homes.py tests/golden/cc_register_homes_baseline.json
git commit -m "$(cat <<'EOF'
test(cc): freeze per-function register-home golden (parity oracle)

Snapshot the heuristic's {var: register} homes per function and add a
corpus-wide parity gate. The allocator (PR 2) must reproduce this map;
byte-neutral identity churn is allowed via a documented exceptions list.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Build the pure allocator-input adapter

A new module that maps a function's economics + target model into the engine's `allocatable` / `CostModel` / `RegisterConstraints` and the `interference` dict. No wiring into emission yet.

**Files:**
- Create: `cc/codegen/x86/regalloc_inputs.py`
- Create: `tests/unit/test_cc_regalloc_inputs.py`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/unit/test_cc_regalloc_inputs.py`:

```python
"""The adapter maps cc economics into cc.regalloc engine inputs."""

from cc.codegen.x86.generator import AutoPinEconomics
from cc.codegen.x86.regalloc_inputs import build_allocator_inputs


def test_save_cost_uses_clobber_minus_elision_and_bp_index_penalty() -> None:
    economics = AutoPinEconomics(
        allocatable=frozenset({"hot"}),
        reference_counts={"hot": 9},
        index_uses={"hot": 2},
        pre_store_clobbers={"hot": {"bx": 1}},
    )
    inputs = build_allocator_inputs(
        economics=economics,
        interference={"hot": set()},
        pool=("bx", "cx", "di", "bp"),
        precolored={},
        register_clobber_counts={"bx": 3, "cx": 4, "di": 0, "bp": 0},
        byte_pool=frozenset({"bx", "cx"}),
    )
    # bx: clobber 3 minus 1 elided = 2.  bp: zero clobber, index penalty 2.
    assert inputs.costs.register_save_cost["hot"]["bx"] == 2
    assert inputs.costs.register_save_cost["hot"]["bp"] == 2
    assert inputs.costs.register_save_cost["hot"]["di"] == 0
    assert inputs.costs.spill_benefit["hot"] == 9
    assert inputs.allocatable == frozenset({"hot"})


def test_byte_typed_value_is_restricted_to_byte_aliasable_registers() -> None:
    economics = AutoPinEconomics(
        allocatable=frozenset({"ch"}),
        reference_counts={"ch": 5},
        byte_typed=frozenset({"ch"}),
    )
    inputs = build_allocator_inputs(
        economics=economics,
        interference={"ch": set()},
        pool=("bx", "cx", "di", "bp"),
        precolored={},
        register_clobber_counts={"bx": 0, "cx": 0, "di": 0, "bp": 0},
        byte_pool=frozenset({"bx", "cx"}),
    )
    assert inputs.constraints.allowed["ch"] == frozenset({"bx", "cx"})


def test_interference_is_restricted_to_allocatable() -> None:
    economics = AutoPinEconomics(allocatable=frozenset({"a", "b"}), reference_counts={"a": 2, "b": 2})
    inputs = build_allocator_inputs(
        economics=economics,
        interference={"a": {"b", "tmp"}, "b": {"a"}, "tmp": {"a"}},
        pool=("bx", "cx"),
        precolored={},
        register_clobber_counts={"bx": 0, "cx": 0},
        byte_pool=frozenset({"bx", "cx"}),
    )
    assert inputs.interference == {"a": {"b"}, "b": {"a"}}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc_inputs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'cc.codegen.x86.regalloc_inputs'`.

- [ ] **Step 3: Implement the adapter**

Create `cc/codegen/x86/regalloc_inputs.py`:

```python
"""Translate cc.py's auto-pin economics into cc.regalloc engine inputs.

PR 2 colors user locals/params only.  The economics (reference counts, per-
register clobber save costs, BP index penalty, byte-alias legality) come from
the AST; interference comes from the AST LivenessAnalyzer.  The result feeds
cc.regalloc.color() directly (no ir.Function required).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from cc import regalloc

if TYPE_CHECKING:
    from cc.codegen.x86.generator import AutoPinEconomics


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class AllocatorInputs:
    """Everything cc.regalloc.color() needs for one function's locals/params."""

    allocatable: frozenset[str]
    constraints: regalloc.RegisterConstraints
    costs: regalloc.CostModel
    interference: dict[str, set[str]]


def build_allocator_inputs(
    *,
    byte_pool: frozenset[str],
    economics: AutoPinEconomics,
    interference: dict[str, set[str]],
    pool: tuple[str, ...],
    precolored: dict[str, str],
    register_clobber_counts: dict[str, int],
) -> AllocatorInputs:
    """Build the engine inputs for *economics* over *pool*.

    ``byte_pool`` is the subset of *pool* with an 8-bit alias (AL/BL/CL/DL).
    ``register_clobber_counts`` is the per-function {register: clobbering-call
    count}; ``pool`` is ordered by ascending clobber cost (the colorer's
    tiebreak).  Save cost for a register is its clobber count minus the
    candidate's pre-first-store elided clobbers; for the frame-pointer register
    (zero clobber) it is the candidate's subscript count (the per-index
    ``mov si, bp`` penalty).
    """
    allocatable = economics.allocatable
    base_register = pool[-1] if pool and register_clobber_counts.get(pool[-1], 0) == 0 else None

    register_save_cost: dict[str, dict[str, int]] = {}
    for name in allocatable:
        per_register: dict[str, int] = {}
        elided = economics.pre_store_clobbers.get(name, {})
        for register in pool:
            if register == base_register:
                per_register[register] = economics.index_uses.get(name, 0)
            else:
                per_register[register] = max(0, register_clobber_counts.get(register, 0) - elided.get(register, 0))
        register_save_cost[name] = per_register

    spill_benefit = {name: economics.reference_counts.get(name, 0) for name in allocatable}

    allowed: dict[str, frozenset[str]] = {}
    for name in economics.byte_typed & allocatable:
        allowed[name] = frozenset(register for register in pool if register in byte_pool)

    restricted: dict[str, set[str]] = {
        name: {neighbor for neighbor in neighbors if neighbor in allocatable}
        for name, neighbors in interference.items()
        if name in allocatable
    }
    for name in allocatable:
        restricted.setdefault(name, set())

    return AllocatorInputs(
        allocatable=allocatable,
        constraints=regalloc.RegisterConstraints(allowed=allowed, pool=pool, precolored=dict(precolored)),
        costs=regalloc.CostModel(register_save_cost=register_save_cost, spill_benefit=spill_benefit),
        interference=restricted,
    )
```

- [ ] **Step 4: Run the unit tests**

Run: `python3 -m pytest tests/unit/test_cc_regalloc_inputs.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full unit suite + byte gate (no wiring → no change)**

Run: `python3 -m pytest tests/unit/ -q && python3 tests/test_cc_function_sizes.py`
Expected: PASS, byte gate unchanged.

- [ ] **Step 6: Commit**

```bash
git add cc/codegen/x86/regalloc_inputs.py tests/unit/test_cc_regalloc_inputs.py
git commit -m "$(cat <<'EOF'
feat(cc): allocator-input adapter (economics -> regalloc CostModel/constraints)

Pure translation of auto-pin economics + AST interference into the inputs
cc.regalloc.color() consumes: save cost = clobber-minus-elision (and the BP
index penalty), spill benefit = reference count, byte-alias allowed sets,
interference restricted to allocatable. Not yet wired into emission.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Wire the allocator behind a flag and map its result

`generate_function` computes homes via `cc.regalloc.color()` when `self.use_regalloc` is set, mapping `homes` → `pinned_register` and leaving spilled locals/params to the existing frame-slot path. Default stays the heuristic.

**Files:**
- Modify: `cc/codegen/x86/generator.py` (`__init__`: read the flag; add `_allocator_homes`)
- Modify: `cc/codegen/x86/emission.py` (`generate_function`: choose allocator vs heuristic)

- [ ] **Step 1: Add the flag**

In `X86CodeGenerator.__init__`, near the other env reads, add:

```python
self.use_regalloc: bool = os.environ.get("BBOE_REGALLOC") == "1"
```

(`import os` already present in `generator.py`; confirm with `grep -n "^import os" cc/codegen/x86/generator.py`.)

- [ ] **Step 2: Write the failing test**

Add to `tests/unit/test_cc_regalloc_inputs.py`:

```python
def test_allocator_homes_match_heuristic_on_a_loop_counter(monkeypatch) -> None:
    """End-to-end: allocator and heuristic agree on a hot loop counter's home."""
    import os

    from cc.codegen.x86.emission import compile_source_homes

    source = next(iter(sorted((__import__("pathlib").Path(__file__).resolve().parents[2] / "user" / "libbboeos").glob("*.c"))))

    monkeypatch.delenv("BBOE_REGALLOC", raising=False)
    heuristic = compile_source_homes(source=source)
    monkeypatch.setenv("BBOE_REGALLOC", "1")
    allocator = compile_source_homes(source=source)

    # Every function the heuristic gave homes to must get the same homes from
    # the allocator (PR 2 parity target; byte-neutral identity churn handled by
    # the corpus golden's exceptions list, not asserted strictly here).
    for function, homes in heuristic.items():
        assert allocator.get(function) == homes, function
```

> This test encodes the parity target. It is expected to FAIL for some functions until Task 5 converges; mark it `@pytest.mark.xfail(strict=False)` during Tasks 4–5 and remove the marker in Task 6 once parity holds.

- [ ] **Step 3: Implement `_allocator_homes`**

Add to `X86CodeGenerator` (near `_select_auto_pin_candidates`):

```python
def _allocator_homes(self, *, body: list[Node], parameters: list, precolored: dict[str, str], apply_liveness_elision: bool = True) -> dict[str, str]:
    """Color locals/params with cc.regalloc; return {name: register} homes.

    Drop-in replacement for :meth:`_select_auto_pin_candidates`: same economics,
    but coloring (which generalizes the heuristic's primary + sharing passes)
    decides homes.  Interference comes from the AST LivenessAnalyzer; on
    LivenessAnalysisError every allocatable value is treated as mutually
    interfering (no illegal sharing), and the byte gate catches any cost.
    """
    if not self.safe_pin_registers:
        return {}
    economics = self._compute_pin_economics(body=body, parameters=parameters, apply_liveness_elision=apply_liveness_elision)
    if not economics.allocatable:
        return {}
    try:
        interference = LivenessAnalyzer(body=body, parameters=parameters).interference()
    except LivenessAnalysisError:
        names = list(economics.allocatable)
        interference = {name: set(economics.allocatable) - {name} for name in names}

    pool = tuple(self.safe_pin_registers)
    byte_pool = frozenset(register for register in pool if register in self.target.LOW_BYTE)
    inputs = build_allocator_inputs(
        byte_pool=byte_pool,
        economics=economics,
        interference=interference,
        pool=pool,
        precolored=precolored,
        register_clobber_counts=self.register_clobber_counts,
    )
    allocation = regalloc.color(
        constraints=inputs.constraints,
        costs=inputs.costs,
        interference=inputs.interference,
        moves=set(),
    )
    return {name: register for name, register in allocation.homes.items() if name in economics.allocatable}
```

Add imports at the top of `generator.py` if missing: `from cc import regalloc` and `from cc.codegen.x86.regalloc_inputs import build_allocator_inputs` (confirm `LivenessAnalyzer`/`LivenessAnalysisError` are already imported — they are, used by the sharing pass). Confirm `self.target.LOW_BYTE` is the right accessor with `grep -n "LOW_BYTE" cc/target.py cc/codegen/x86/*.py`; if `LOW_BYTE` is a module-level dict in `cc/target.py`, import it directly instead.

- [ ] **Step 4: Choose allocator vs heuristic in `generate_function`**

In `cc/codegen/x86/emission.py`, replace the `_select_auto_pin_candidates` call (lines 3353–3355) with a branch. The `precolored` map is the already-decided explicit/`in_register` pins captured just before this point:

```python
        precolored_homes = {
            param.name: param.in_register for param in parameters if param.in_register is not None and function.naked
        }
        if self.use_regalloc:
            self.auto_pin_candidates = self._allocator_homes(
                body=body, parameters=param_candidates, precolored=precolored_homes, apply_liveness_elision=name != "main"
            )
        else:
            self.auto_pin_candidates = self._select_auto_pin_candidates(
                body=body, parameters=param_candidates, apply_liveness_elision=name != "main"
            )
```

The downstream `scan_locals` consumes `self.auto_pin_candidates` identically, so spilled values (those absent from the returned map) fall through to `allocate_local` exactly as today. No change needed below.

> The non-naked `in_register` params keep their existing slot-spill handling (lines 3357–3380), so they are intentionally excluded from `precolored_homes`; the allocator never sees them as allocatable because they are params spilled to slots, not pin candidates.

- [ ] **Step 5: Run unit tests in both modes**

Run: `python3 -m pytest tests/unit/ -q`
Then: `BBOE_REGALLOC=1 python3 -m pytest tests/unit/test_cc_regalloc_inputs.py -q`
Expected: default PASS; allocator-mode end-to-end test xfails on not-yet-converged functions (acceptable this task).

- [ ] **Step 6: Baseline the divergence (informational)**

Run: `BBOE_REGALLOC=1 python3 tests/test_cc_register_homes.py || true` and `BBOE_REGALLOC=1 python3 tests/test_cc_function_sizes.py || true`
Record which functions diverge in homes and which regress bytes — this is the Task 5 worklist. Do **not** refresh any golden here.

- [ ] **Step 7: Commit**

```bash
git add cc/codegen/x86/generator.py cc/codegen/x86/emission.py tests/unit/test_cc_regalloc_inputs.py
git commit -m "$(cat <<'EOF'
feat(cc): wire regalloc.color() behind BBOE_REGALLOC for locals/params

generate_function uses _allocator_homes (AST-liveness interference +
economics-derived CostModel/constraints) when BBOE_REGALLOC=1; default
stays the heuristic. homes -> pinned_register, spilled -> frame slots via
the unchanged scan_locals path.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Converge to byte parity

Iterate (allocator mode) until the byte gate is green and the home golden matches or each diff is byte-neutral and documented. This task is empirical: the byte gate and the home golden are the oracles.

**Files:**
- Modify (as needed): `cc/codegen/x86/regalloc_inputs.py`, `cc/codegen/x86/generator.py` (`_allocator_homes`)
- Modify: `tests/test_cc_register_homes.py` (`IDENTITY_EXCEPTIONS`)

- [ ] **Step 1: Enumerate the gaps**

Run: `BBOE_REGALLOC=1 python3 tests/test_cc_function_sizes.py` and `BBOE_REGALLOC=1 python3 tests/test_cc_register_homes.py`.
List every function that (a) grew bytes, or (b) changed homes. Classify each: byte-neutral identity churn (DI↔SI-class swap, same size) vs a real regression (fewer pins, wrong spill, BP misplacement).

- [ ] **Step 2: Tune the cost model against each regression class**

Apply the matching fix in `build_allocator_inputs` / `_allocator_homes`, re-run the two gates after each change:

- **BP placed on an index-heavy var** → confirm the base register's `register_save_cost = index_uses` term is applied and that `pool` puts the base register last (it is, via `compute_safe_pin_registers`). A var with `index_uses > best_other_clobber` must not pick BP.
- **A var spilled that the heuristic pinned** → check the benefit gate: `spill_benefit (refs) > save_cost (effective clobber)` must hold; verify pre-store elision is being subtracted (`pre_store_clobbers`). If the heuristic's "continue past a failed candidate" behavior matters, confirm coloring tries the next-cheapest legal register (it does — `color()` picks the min-save-cost legal register, not a fixed zip).
- **A var pinned that the heuristic spilled** → coloring is more aggressive because sharing freed a register. If byte-neutral or smaller, add it to `IDENTITY_EXCEPTIONS` with a one-line reason. If larger, raise its effective save cost (it should not have been eligible — re-check `allocatable`).
- **Byte-neutral register-identity swap** → add `{source: {function: "DI/SI-class identity swap, byte-equal"}}` to `IDENTITY_EXCEPTIONS`.

- [ ] **Step 3: Converge the byte gate to green**

Run: `BBOE_REGALLOC=1 python3 tests/test_cc_function_sizes.py`
Expected: PASS (no function grew). For any residual straggler that cannot reach `≤ baseline`, confirm the regression is small, record the exact byte delta and a one-line justification in the PR description, and proceed (the agreed re-bless policy). Do **not** silently refresh the size baseline here — stragglers are re-blessed explicitly in Task 6 with their justification.

- [ ] **Step 4: Run the 16/32-bit legality gate in allocator mode**

Run: `BBOE_REGALLOC=1 python3 tests/test_cc_bits.py`
Expected: PASS. A failure means a byte value was homed in a non-aliasable register or an index legality issue — fix `allowed` in the adapter (`byte_pool` restriction) and re-run.

- [ ] **Step 5: Commit the converged adapter**

```bash
git add cc/codegen/x86/regalloc_inputs.py cc/codegen/x86/generator.py tests/test_cc_register_homes.py
git commit -m "$(cat <<'EOF'
fix(cc): converge regalloc locals/params homes to byte parity

Tune save-cost/benefit/byte-alias inputs until BBOE_REGALLOC byte gate is
green and home golden matches (byte-neutral identity churn documented in
IDENTITY_EXCEPTIONS). Stragglers, if any, justified in the PR description.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Flip the default and delete the heuristic

Make the allocator the only path, remove the flag, delete `_select_auto_pin_candidates` and its now-unused helpers, regenerate the golden from the allocator, and re-bless any churned goldens.

**Files:**
- Modify: `cc/codegen/x86/generator.py` (delete `_select_auto_pin_candidates`, `_tally_auto_pin_counts`, `_tally_pre_store_clobbers`, `_rank_candidates`, `_collect_auto_pin_body_candidates`, `AutoPinTallyState`, and the sharing-pass code if not shared with `_compute_pin_economics`)
- Modify: `cc/codegen/x86/emission.py` (remove the branch; call `_allocator_homes` unconditionally; drop `self.use_regalloc`)
- Modify: `tests/unit/test_cc_regalloc_inputs.py` (remove the `xfail` marker)
- Regenerate: `tests/golden/cc_register_homes_baseline.json`; re-bless `tests/test_cc_place.py` golden if churned

- [ ] **Step 1: Make the allocator unconditional**

In `generate_function`, replace the Task-4 branch with the single call:

```python
        precolored_homes = {
            param.name: param.in_register for param in parameters if param.in_register is not None and function.naked
        }
        self.auto_pin_candidates = self._allocator_homes(
            body=body, parameters=param_candidates, precolored=precolored_homes, apply_liveness_elision=name != "main"
        )
```

Remove `self.use_regalloc` from `__init__`.

- [ ] **Step 2: Delete the heuristic and its now-dead helpers**

Delete `_select_auto_pin_candidates` (4540–4729) and the helpers used *only* by it. Verify each helper is dead before deleting:

```bash
for sym in _tally_auto_pin_counts _tally_pre_store_clobbers _rank_candidates _collect_auto_pin_body_candidates _is_candidate_expression_temporary AutoPinTallyState; do
  echo "== $sym =="; rtk proxy grep -rn "$sym" cc/ tests/
done
```

`_compute_pin_economics` (Task 1) still uses `_tally_auto_pin_counts`, `_tally_pre_store_clobbers`, `_collect_auto_pin_body_candidates`, `_is_candidate_expression_temporary`, and `AutoPinTallyState` — those stay. Delete only symbols with zero remaining references (`_select_auto_pin_candidates`, `_rank_candidates` if now unused, and the sharing-pass block, which lived inside `_select_auto_pin_candidates`).

- [ ] **Step 3: Remove the xfail marker and assert parity**

In `tests/unit/test_cc_regalloc_inputs.py`, remove the `@pytest.mark.xfail` marker from `test_allocator_homes_match_heuristic_on_a_loop_counter` (the home comparison now runs in the single, default mode — drop the `BBOE_REGALLOC` toggling; compile once and compare against the committed golden).

- [ ] **Step 4: Regenerate the golden from the allocator**

Run: `BBOE_UPDATE_HOMES=1 python3 tests/test_cc_register_homes.py && python3 tests/test_cc_register_homes.py`
Expected: parity OK (the golden now reflects the allocator; identity exceptions become unnecessary — empty the `IDENTITY_EXCEPTIONS` dict if the regenerated golden is self-consistent).

- [ ] **Step 5: Re-bless the Place golden if it churned**

Run: `python3 tests/test_cc_place.py`
If it fails on the assignment-shape golden only: `BBOE_UPDATE_GOLDEN=1 python3 tests/test_cc_place.py`, then re-run to confirm green. Inspect the diff to confirm it is register-home churn, not a correctness change.

- [ ] **Step 6: Run unit suite + byte gate (now default)**

Run: `python3 -m pytest tests/unit/ -q && python3 tests/test_cc_function_sizes.py`
Expected: PASS. For re-blessed stragglers from Task 5, refresh the size baseline now and note each in the commit body:
`BBOE_UPDATE_SIZES=1 python3 tests/test_cc_function_sizes.py` (only if stragglers were agreed).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(cc)!: regalloc is the sole authority for locals/params; delete auto-pin

Flip generate_function to _allocator_homes unconditionally; remove
BBOE_REGALLOC and _select_auto_pin_candidates + the greedy ranking/sharing
pass. Coloring (cc.regalloc) now decides every local/param home, fed by AST
liveness + the retained economics. Golden regenerated from the allocator;
test_cc_place golden re-blessed (home churn only). Straggler byte re-blesses,
if any, itemized below.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Full local CI matrix and finish the branch

**Files:** none (verification + branch finish)

- [ ] **Step 1: Run the correctness matrix locally**

Per the "run full CI matrix locally on big changes" rule, run each and confirm PASS:

```bash
python3 -m pytest tests/unit/ -q
python3 tests/test_cc_function_sizes.py
python3 tests/test_cc_register_homes.py
python3 tests/test_cc_bits.py
python3 tests/test_cc_place.py
python3 tests/test_cc_casts.py
python3 tests/test_cc_bitfields.py
python3 tests/test_cc_local_structs.py
python3 tests/test_cc_compatibility.py
python3 tests/test_asm.py
python3 tests/test_archive.py
python3 tests/test_kernel_archive.py
python3 tests/test_programs.py --slow
python3 tests/test_programs.py --filesystem ext2 --slow
python3 tests/test_bboefs.py
```

Expected: all PASS. Investigate any failure before proceeding — a runtime failure in `test_asm`/`test_programs` means a missed call-clobber save (a homed local live across a call not pushed/popped), which is a miscompile, not a byte issue.

- [ ] **Step 2: Run pre-commit hooks**

Run: `pre-commit run --files $(git diff --name-only origin/main...HEAD)`
Expected: ruff/format/whitespace hooks PASS (fix and amend if any reformat).

- [ ] **Step 3: Finish the branch**

Invoke `superpowers:finishing-a-development-branch` and choose "Push and create a Pull Request". PR body must:
- summarize the locals/params → coloring switch and the auto-pin deletion;
- list any re-blessed straggler functions with their exact byte deltas and one-line justifications;
- note the `test_cc_place` golden re-bless (home churn only);
- end with the required PR footer.

---

## Self-Review notes (author)

- **Spec coverage:** scope/end-state → Tasks 4,6; cost-input seam → Tasks 1,3; allocation-orthogonal-to-emission → Task 4 (calls `color()` directly, emission untouched); golden harness → Task 2; AST-liveness with conservative fallback → Task 4 Step 3; cutover sequence → Tasks 1–6; re-bless policy → Task 5 Step 3 + Task 6 Step 6; full matrix → Task 7.
- **Known empirical risk:** Tasks 1 (exact ranking parity) and 5 (full byte parity) are the convergence-heavy steps; both have the byte gate as the deterministic oracle and the documented re-bless escape hatch.
- **Seam to confirm during Task 2:** the programmatic `compile_*_to_generator` entry — factor the minimal shared seam from the existing size-gate compile path rather than duplicating parse→lower→generate.
