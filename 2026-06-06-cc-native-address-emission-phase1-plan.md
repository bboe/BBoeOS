# cc.py Native-Address Emission Phase 1 (Flip-Over) Implementation Plan

**For agentic workers:** REQUIRED SUB-SKILL: Use
superpowers:subagent-driven-development (recommended) or
superpowers:executing-plans to implement this plan task-by-task. Steps use
checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the AST re-seat in cc.py's IR access-op emission with a native
AddressPlan layer — byte-gate 0-delta — per the approved design
([2026-06-06-cc-native-address-emission-design.md](./2026-06-06-cc-native-address-emission-design.md)).

**Architecture:** A pure `AddressPlan` dataclass (new module
`cc/codegen/address_plan.py`) captures the resolved form of one `ir.Address`:
base, dynamic-index terms with strides, folded displacement, and
terminal-shaping facts. A planner derives plans from `Address.shape` using the
generator's pure layout helpers; a materializer turns plans into
`MemoryOperand`s using the generator's existing *emitting* helpers. The five IR
terminal cases (`Load`/`Store`/`AddressOf`/`IncrementDecrement`/`IndirectCall`)
consume plans instead of rebuilding AST nodes. Fixed BX/SI scratch is retained;
clobber facts are declared but unconsumed (phase 2 wires them into regalloc).

**Tech Stack:** Python 3 (cc.py compiler), NASM verification via the byte gate,
pytest unit tests, QEMU program tests.

---

## The core implementation strategy: split, don't duplicate

`resolve_address` (cc/codegen/x86/generator.py:6478) and its helpers mix two
concerns:

1. **Pure derivation** — member offsets, strides, widths, base kinds, bitfield
   info, decay flags. No emitted code.
2. **Emission** — dynamic-index evaluation/scaling (`_accumulate_subscript`,
   generator.py:440), pointer-base loads (`_load_member_base`), Horner walks.

The planner is the pure half; the materializer is the emitting half. **Where a
legacy helper mixes both, refactor it in place into a pure-derivation function
plus an emitting function, then call the pure half from the planner and the
emitting half from the materializer.** Byte parity then holds by construction —
the same emitting code runs, fed by pre-derived facts. Never transcribe an
instruction sequence into a second copy; if you find yourself copying
`emit(...)` lines, extract the original into a shared method instead.

The shared terminal emitters survive unchanged and are reused as-is:
`_emit_resolved_load` (generator.py:3280), `_emit_field_load`
(generator.py:1788), `_emit_field_store`, `_emit_store_accumulator_at_width`
(generator.py:3315), `_emit_resolved_field_store`, `_emit_member_address`,
`_emit_bitfield_read` / `_emit_bitfield_write_literal`.

## Context for the implementing engineer

- **Working directory:** `/home/ubuntu/bboeos/.claude/worktrees/next` (a git
  worktree). Never `cd` to the original repo root. `main` is checked out in
  another worktree, so `git checkout main` fails — branch from `origin/main`.
- **Branch:** `git fetch origin && git checkout -b bboe/cc-native-address-phase1
  origin/main`
- **Conventions (mandatory):** no abbreviations in any identifier (`expression`
  not `expr`, `displacement` not `disp`); functions, dataclass fields,
  match-case arms, and isinstance tuples sorted alphabetically within their
  scope; preserve existing comments; commits end with `Co-Authored-By: Claude
  Opus 4.8 <noreply@anthropic.com>`.
- **RTK quirk:** the shell proxy sometimes mangles `grep -n` output — use `awk
  '/pattern/ {print NR": "$0}' file` instead.
- **Verification commands** (run from the worktree root):
  - Byte gate: `python3 tests/test_cc_function_sizes.py` — must print `PASS
    per-function byte-size gate (361 functions, 49 files)`. Any `GREW` line is a
    hard failure. Never set `BBOE_UPDATE_SIZES=1` in this phase — phase 1 is
    0-delta by definition.
  - Place golden: `python3 tests/test_cc_place.py` — byte-identical asm against
    `tests/golden/cc_place_*.asm`. A benign label renumber may be re-blessed
    ONLY if the diff shows zero instruction changes (document it in the commit
    message).
  - 16/32-bit matrix: `python3 tests/test_cc_bits.py` — expect `122/122`.
  - Unit tests: `python3 -m pytest tests/unit/test_cc_address_plan.py
    tests/unit/test_cc_ir.py tests/unit/test_cc_codegen.py -q` (never point
    pytest at the bare `tests/` directory — the top-level `test_*.py` files are
    QEMU drivers, not pytest tests).
  - Per-commit gate bundle (run after EVERY cutover task): `python3
    tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py && python3
    tests/test_cc_bits.py`
- **Corpus census** (how to count remaining legacy-path Address consumers):

```bash
python3 - <<'EOF'
import glob
from pathlib import Path
from cc import ir
from cc.cli import _discover_include_paths
from cc.lexer import tokenize
from cc.parser import Parser
from cc.preprocessor import apply_defines, preprocess

total_addresses = 0
for pattern in ("user/libbboeos/*.c", "user/programs/*.c"):
    for path in sorted(glob.glob(pattern)):
        source = Path(path).read_text()
        lines = preprocess(source, bits=32, include_base=str(Path(path).parent), search_paths=_discover_include_paths(Path(path)))
        tokens = apply_defines(tokenize("\n".join(lines)))
        program = Parser(tokens, bits=32).parse_program()
        ir_program = ir.Builder().build_program(program)
        for function in ir_program.functions:
            for instruction in function.body:
                if isinstance(instruction, ir.Address):
                    total_addresses += 1
print(f"{total_addresses} ir.Address ops in corpus")
EOF
```

## File structure

- **Create:** `cc/codegen/address_plan.py` — `AddressTerm` + `AddressPlan`
  dataclasses and the pure scale-classification helper. No imports from codegen
  internals (pure data; `FieldInfo` import only).
- **Create:** `tests/unit/test_cc_address_plan.py` — planner unit coverage.
- **Modify:** `cc/codegen/x86/emission.py` — `_plan_ir_address` (planner),
  `_materialize_address_plan` (materializer), the five terminal cases in
  `_lower_ir_instruction` (emission.py:1949), deletion of
  `_ir_address_with_index` (emission.py:1830) and
  `_reseat_nested_subscript_indices`.
- **Modify:** `cc/codegen/x86/generator.py` — extract
  `_store_accumulator_to_local` from `emit_store_local` (generator.py:6396);
  split mixed pure/emitting helpers (`_resolve_member_place_info`
  generator.py:4785, `_emit_multidim_subscript_address` generator.py:2503,
  `_emit_multidim_member_address` generator.py:2411,
  `_emit_pointer_to_array_address` generator.py:3012, `_resolve_member_index`
  generator.py:4673) into pure + emitting halves.
- **Modify:** `cc/ir.py` — docstring pointer updates only (no IR shape changes
  in phase 1).

Plans are recorded per function in a new dict `self._ir_address_plans: dict[str,
AddressPlan]` (keyed by the Address op's `destination`), alongside the existing
`self._ir_address_ops` dict; the legacy dict and the plan dict coexist until
Task 9 deletes the legacy path. Planning happens at the `case ir.Address` site
in `_lower_ir_instruction` — locals and layouts are registered by then, and
planning is pure (no code is emitted at the Address site; phase 1 keeps every
plan in folded mode). The standalone pre-pass form arrives with 3c, which needs
plans before emission for CSE.

---

### Task 1: AddressPlan dataclasses

**Files:**
- Create: `cc/codegen/address_plan.py`
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the pure AddressPlan dataclasses and helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cc.codegen.address_plan import AddressPlan, AddressTerm, scale_encodes_in_operand  # noqa: E402


def test_address_plan_defaults():
    plan = AddressPlan(base="ebp-8", base_kind="frame")
    assert plan.bitfield is None
    assert plan.clobbers == frozenset()
    assert plan.decay_to_address is False
    assert plan.displacement == 0
    assert plan.terms == ()


def test_address_term_carries_value_and_scale():
    term = AddressTerm(index_value="_ir_3", scale=4)
    assert term.index_value == "_ir_3"
    assert term.scale == 4


def test_scale_encodes_in_operand_32_bit():
    assert scale_encodes_in_operand(bits=32, scale=1)
    assert scale_encodes_in_operand(bits=32, scale=4)
    assert scale_encodes_in_operand(bits=32, scale=8)
    assert not scale_encodes_in_operand(bits=32, scale=3)
    assert not scale_encodes_in_operand(bits=32, scale=32)


def test_scale_never_encodes_in_operand_16_bit():
    # 16-bit addressing has no SIB byte: a scale-1 index register
    # ([bx+si]) participates, but scaling never encodes.
    assert scale_encodes_in_operand(bits=16, scale=1)
    assert not scale_encodes_in_operand(bits=16, scale=2)
    assert not scale_encodes_in_operand(bits=16, scale=4)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q` Expected: FAIL —
`ModuleNotFoundError: No module named 'cc.codegen.address_plan'`

- [ ] **Step 3: Write the module**

```python
"""Resolved, target-aware address plans for ``ir.Address`` operations.

An :class:`AddressPlan` is the pure, post-layout form of one ``ir.Address``:
the AST ``shape``'s job ends when the planner produces a plan, and emission
materializes the plan into a :class:`~cc.codegen.x86.generator.MemoryOperand`
without ever re-walking AST. Design:
``design-specs:2026-06-06-cc-native-address-emission-design.md``.

Phase 1 keeps every plan in *folded* mode (the producing ``Address`` op emits
nothing; the consuming terminal absorbs the materialization). The
``clobbers`` field is a declared fact for the register allocator — computed
by the planner, unconsumed until phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from cc.codegen.base import FieldInfo


@dataclass
class AddressTerm:
    """One dynamic subscript: ``index_value`` scaled by ``scale`` bytes."""

    index_value: Union[int, str]
    scale: int


@dataclass
class AddressPlan:
    """The resolved form of one ``ir.Address`` (see module docstring).

    ``base_kind`` selects how ``base`` reads:

    - ``"frame"`` — ``base`` is a frame-relative string (``"ebp-8"``).
    - ``"label"`` — ``base`` is a NASM label (``"_g_table"``).
    - ``"plan"`` — ``base`` is a nested :class:`AddressPlan` whose
      materialization (a decayed struct-value member address) seeds the base
      register; used by chained dot members (``a.b.c``).
    - ``"pointer"`` — ``base`` is a named pointer variable; materialization
      loads it via the shared SI-or-BX base load (``_load_member_base``).

    ``base_is_static`` / ``base_preserves_accumulator`` capture the store
    orderings of ``_emit_member_scalar_resolved_store``; ``horner`` marks a
    multi-term plan whose legacy materialization is the row-major Horner walk
    rather than per-term scale-and-sum.
    """

    base: Union["AddressPlan", str]
    base_kind: str
    base_is_static: bool = True
    base_preserves_accumulator: bool = False
    bitfield: "FieldInfo | None" = None
    clobbers: frozenset[str] = frozenset()
    decay_to_address: bool = False
    displacement: int = 0
    element_size: int = 0
    field_size: int = 0
    horner: bool = False
    line: int = 0
    raw_width: bool = False
    terms: tuple[AddressTerm, ...] = ()


def scale_encodes_in_operand(*, bits: int, scale: int) -> bool:
    """Return True when ``scale`` folds into a memory operand at ``bits``.

    32-bit SIB encoding supports scales 1/2/4/8. 16-bit addressing has no
    SIB byte: an index register participates only unscaled.
    """
    if bits == 16:
        return scale == 1
    return scale in (1, 2, 4, 8)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q` Expected: PASS (4
tests)

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/address_plan.py tests/unit/test_cc_address_plan.py
git commit -m "feat(cc): add pure AddressPlan dataclasses for native Address emission (phase 1)"
```

---

### Task 2: extract the store-to-local tail from emit_store_local

The native `ir.Load` terminal must run the exact accumulator-to-local store tail
`emit_store_local` runs after evaluating a `PlaceLoad` expression
(pinned-register move-out, byte-scalar `al` store, `ax_local` tracking,
peephole-strand invalidation). Extract it so both paths share one body.

**Files:**
- Modify: `cc/codegen/x86/generator.py:6396-6476` (`emit_store_local`)

- [ ] **Step 1: Extract the helper**

In `emit_store_local`, the tail is everything from `if direct_register is not
None:` (generator.py:6441) through the `_peephole_will_strand_ax` invalidation
(generator.py:6476). Move it verbatim into a new method (inserted in
alphabetical order among the generator's methods):

```python
def _store_accumulator_to_local(self, name: str, /, *, direct_register: str | None) -> None:
    """Store the accumulator into local *name* (pinned register / byte / word tail).

    The shared tail of :meth:`emit_store_local`, extracted so the native
    ``ir.Load`` terminal can run the byte-identical store + AX-tracking
    sequence after materializing an :class:`AddressPlan` load.
    """
    # <lines 6441-6476 of emit_store_local, moved verbatim, including all comments>
```

`emit_store_local` then ends with:

```python
        previous_store_target = self.store_target_register
        self.store_target_register = direct_register
        self.generate_expression(expression)
        self.store_target_register = previous_store_target
        self._store_accumulator_to_local(name, direct_register=direct_register)
```

This is a pure code move — no logic change of any kind.

- [ ] **Step 2: Run the verification bundle**

Run: `python3 tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py
&& python3 tests/test_cc_bits.py && python3 -m pytest
tests/unit/test_cc_codegen.py -q` Expected: byte gate PASS (361 functions, 49
files), golden byte-identical, 122/122, unit PASS.

- [ ] **Step 3: Commit**

```bash
git add cc/codegen/x86/generator.py
git commit -m "refactor(cc): extract _store_accumulator_to_local from emit_store_local (byte-neutral)"
```

---

### Task 3: planner + materializer + native ir.Load for dot-member scalars

The smallest real slice: `obj.field` loads (VariablePlace-rooted MemberPlace, no
subscripts) flow Address→plan→MemoryOperand→`_emit_resolved_load` natively.
Everything else keeps the legacy re-seat path.

**Files:**
- Modify: `cc/codegen/x86/emission.py` (planner, materializer, `ir.Load` case)
- Modify: `cc/codegen/x86/generator.py` (split `_resolve_member_place_info`)
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_cc_address_plan.py` a compile-and-inspect harness. Model
the pipeline setup on `_compile` in `tests/unit/test_cc_codegen.py:65` but keep
(and return) the generator instance so plans are inspectable:

```python
def _generate(source_text: str, /, *, bits: int = 32):
    """Compile *source_text* and return the generator (plans inspectable)."""
    from cc.codegen.x86.generator import X86CodeGenerator
    from cc.lexer import tokenize
    from cc.options import CompilerOptions
    from cc.parser import Parser
    from cc.preprocessor import apply_defines, preprocess

    lines = preprocess(source_text, bits=bits)
    tokens = apply_defines(tokenize("\n".join(lines)))
    program = Parser(tokens, bits=bits).parse_program()
    options = CompilerOptions(bits=bits)
    generator = X86CodeGenerator(options=options)
    generator.generate(program)
    return generator
```

(Adjust the constructor/`generate` call signatures to match
`test_cc_codegen.py`'s `_compile` exactly — copy its body, drop the
assembly-text return, return `generator`.)

```python
DOT_MEMBER_SOURCE = """
struct point { int x; int y; };
struct point g;
int main() {
    int value;
    value = g.y;
    return value;
}
"""


def test_dot_member_load_produces_pure_displacement_plan():
    generator = _generate(DOT_MEMBER_SOURCE)
    plans = list(generator._ir_address_plans.values())
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "label"
    assert plan.displacement == 4  # offset of y
    assert plan.terms == ()
    assert plan.field_size == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q` Expected: FAIL —
`AttributeError: ... no attribute '_ir_address_plans'`

- [ ] **Step 3: Split `_resolve_member_place_info` and implement**

(a) In generator.py, split `_resolve_member_place_info` (generator.py:4785) —
its VariablePlace arm (generator.py:4811-4822) is already pure (lookups only, no
`emit`). Extract the pure lookups into:

```python
def _derive_dot_member_layout(self, place: MemberPlace, /) -> tuple[str, "FieldInfo"]:
    """Pure layout derivation for ``obj.field`` (the VariablePlace arm).

    Returns ``(base_operand, info)`` without emitting code — the pure half
    of :meth:`_resolve_member_place_info`'s dot arm, shared by the legacy
    resolver and the AddressPlan planner.
    """
    # <move lines 4811-4822's lookups here verbatim; the original arm
    #  becomes: base_operand, info = self._derive_dot_member_layout(place);
    #  return base_operand, False, info>
```

(b) In emission.py add the planner (alphabetical position, near
`_ir_address_with_index`):

```python
def _plan_ir_address(self, address: ir.Address, /) -> AddressPlan | None:
    """Derive the pure AddressPlan for *address*, or None if not yet plannable.

    Phase-1 coverage grows shape class by shape class; a None return routes
    the op through the legacy AST re-seat path. Planning is PURE — no code
    is emitted here (every phase-1 plan is folded; the consuming terminal
    materializes it).
    """
    shape = address.shape
    if address.indices:
        return None  # dynamic-index shapes arrive in Task 6
    if isinstance(shape, MemberPlace) and isinstance(shape.base, VariablePlace):
        base_operand, info = self._derive_dot_member_layout(shape)
        is_struct_value = info.type_name.startswith("struct ") and not info.type_name.endswith("*")
        base_kind = "label" if shape.base.name in self.global_scalars or shape.base.name in self.global_arrays else "frame"
        return AddressPlan(
            base=base_operand,
            base_kind=base_kind,
            bitfield=info if info.bit_width is not None else None,
            decay_to_address=info.field_size != info.element_size or is_struct_value,
            displacement=info.byte_offset,
            element_size=info.element_size,
            field_size=info.field_size,
            line=shape.line,
        )
    return None
```

NOTE: mirror the `decay_to_address` / bitfield derivation from
`resolve_address`'s MemberPlace arm (generator.py:6510-6522) exactly — read that
arm before writing this. The `base_kind` distinction must reproduce what
`_resolve_struct_value_base` returns (`_g_<name>` label vs `ebp-N` frame
string); derive it from the returned `base_operand` string instead of
re-checking dicts if that is simpler and exact.

(c) The materializer (also emission.py, alphabetical):

```python
def _materialize_address_plan(self, plan: AddressPlan, /) -> MemoryOperand:
    """Emit the plan's deferred base/index code; return the terminal operand.

    Phase-1: pure-displacement plans emit nothing (parity with the legacy
    static-base resolution).
    """
    return MemoryOperand(
        base=plan.base,
        base_kind=plan.base_kind,
        bitfield=plan.bitfield,
        decay_to_address=plan.decay_to_address,
        displacement=plan.displacement,
        element_size=plan.element_size,
        field_size=plan.field_size,
        raw_width=plan.raw_width,
    )
```

(d) Wire the dispatch in `_lower_ir_instruction` (emission.py:1949). The `case
ir.Address` records a plan when plannable:

```python
case ir.Address(destination=destination):
    # Pure structured-reference value: emits no code on its own.
    # Plannable shapes record an AddressPlan (native path); the rest
    # record the op for the legacy AST re-seat (deleted in Task 9).
    plan = self._plan_ir_address(instruction)
    if plan is not None:
        self._ir_address_plans[destination] = plan
    else:
        self._ir_address_ops[destination] = instruction
```

The `case ir.Load` consults plans first:

```python
case ir.Load(address=address, destination=destination):
    plan = self._ir_address_plans.get(address)
    if plan is not None:
        # Native path: materialize the plan and run the shared resolved
        # load + the exact store-to-local tail emit_store_local runs.
        self.ax_clear()
        operand = self._materialize_address_plan(plan)
        self._emit_resolved_load(operand)
        direct_register = self.pinned_register.get(destination) or self.register_aliased_globals.get(destination)
        self._store_accumulator_to_local(destination, direct_register=direct_register)
    else:
        # <the existing legacy body, unchanged>
```

CRITICAL parity notes for (d): compare against what
`emit_store_local(expression=PlaceLoad(...), name=destination)` does today
(generator.py:6396): it checks `global_arrays` assignment errors, `unsigned
long` routing, `_try_direct_load`, and wraps evaluation in
`store_target_register`. IR temp destinations are never `unsigned long` locals,
never global arrays, and `_try_direct_load` rejects `PlaceLoad` expressions —
verify each claim by reading those helpers, then mirror the `ax_clear` placement
of `_emit_place_load`'s member arm (generator.py:2851- 2855: `ax_clear` BEFORE
resolution, terminal's own `ax_clear` after). Also wrap the native body in the
same `store_target_register` set/restore that `emit_store_local` performs if
pinned destinations exist among IR temps (`self.temp_pinned_registers`). If
parity breaks, the byte gate and `tests/golden/cc_place_index_member.asm` show
exactly where.

Add the imports (`AddressPlan`, `AddressTerm`, `scale_encodes_in_operand` from
`cc.codegen.address_plan`) and initialize `self._ir_address_plans = {}`
everywhere `self._ir_address_ops` is reset (search: `awk '/_ir_address_ops/
{print FILENAME":"NR": "$0}' cc/codegen/x86/*.py`).

- [ ] **Step 4: Run tests + the gate bundle**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py
tests/unit/test_cc_codegen.py -q` Expected: PASS. Run: `python3
tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py && python3
tests/test_cc_bits.py` Expected: byte gate PASS with **zero GREW and zero shrank
lines**, golden byte-identical, 122/122.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/address_plan.py cc/codegen/x86/emission.py cc/codegen/x86/generator.py tests/unit/test_cc_address_plan.py
git commit -m "feat(cc): plan + materialize dot-member scalar loads natively (phase 1)"
```

---

### Task 4: arrow and chained member bases (loads)

Extend the planner to `p->field` (named-pointer base) and chained dot (`a.b.c`,
`p->outer.inner`) loads.

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`_plan_ir_address`,
  `_materialize_address_plan`)
- Modify: `cc/codegen/x86/generator.py` (split the arrow arm of
  `_resolve_member_place_info`)
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing tests**

```python
ARROW_MEMBER_SOURCE = """
struct node { int value; struct node *next; };
int read_value(struct node *n) {
    int out;
    out = n->value;
    return out;
}
"""


def test_arrow_member_load_plans_pointer_base():
    generator = _generate(ARROW_MEMBER_SOURCE)
    plans = list(generator._ir_address_plans.values())
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "pointer"
    assert plan.base == "n"
    assert plan.base_preserves_accumulator is True
    assert plan.base_is_static is False


CHAINED_MEMBER_SOURCE = """
struct inner { int depth; };
struct outer { struct inner nested; };
struct outer g;
int main() {
    int out;
    out = g.nested.depth;
    return out;
}
"""


def test_chained_dot_member_load_plans_nested_base():
    generator = _generate(CHAINED_MEMBER_SOURCE)
    plans = list(generator._ir_address_plans.values())
    assert len(plans) == 1
    # g.nested.depth folds statically: one plan, displacement = offset sum.
    assert plans[0].displacement == 0
    assert plans[0].base_kind == "label"
```

NOTE on the chained test: confirm against the CURRENT lowering first — if the IR
builder folds `g.nested.depth` through the static chain (likely: the legacy
`resolve_address` chained-dot arm materializes a register base via
`PlaceLoad(place.base)` only when the base is NOT statically resolvable), adjust
the expected plan to match what the legacy path emits today; the byte gate
defines truth. If chained-dot genuinely needs a register base
(generator.py:4855-4866), the expected plan is `base_kind == "plan"` with a
nested `AddressPlan` whose `decay_to_address` is True.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q` Expected: the two
new tests FAIL (plans dict empty for these shapes — they fell through to the
legacy path).

- [ ] **Step 3: Implement**

(a) Arrow arm (generator.py:4824-4833) is already split pure/emitting:
`_resolve_member_index_layout(arrow=True, ...)` is the pure half (verify it
emits nothing by reading it: generator.py:4753), `_load_member_base` the
emitting half. Planner addition (inside `_plan_ir_address`, keeping arms ordered
by shape specificity matching `_resolve_member_place_info`):

```python
    if (
        isinstance(shape, MemberPlace)
        and isinstance(shape.base, DereferencePlace)
        and isinstance(shape.base.pointer, VariablePlace)
    ):
        info = self._resolve_member_index_layout(
            arrow=True,
            line=shape.line,
            member_name=shape.member_name,
            object_name=shape.base.pointer.name,
        )
        is_struct_value = info.type_name.startswith("struct ") and not info.type_name.endswith("*")
        return AddressPlan(
            base=shape.base.pointer.name,
            base_kind="pointer",
            base_is_static=False,
            base_preserves_accumulator=True,
            bitfield=info if info.bit_width is not None else None,
            decay_to_address=info.field_size != info.element_size or is_struct_value,
            displacement=info.byte_offset,
            element_size=info.element_size,
            field_size=info.field_size,
            line=shape.line,
        )
```

(b) Materializer gains the pointer-base arm:

```python
    if plan.base_kind == "pointer":
        base_register = self._load_member_base(plan.base)
        return MemoryOperand(
            base=base_register,
            base_kind="register",
            ...same field copies as the static arm...,
        )
```

Refactor the operand construction into one local helper inside
`_materialize_address_plan` so the field copies are written once:

```python
def _operand_from_plan(*, base: str, base_kind: str) -> MemoryOperand:
    return MemoryOperand(
        base=base,
        base_kind=base_kind,
        bitfield=plan.bitfield,
        decay_to_address=plan.decay_to_address,
        displacement=plan.displacement,
        element_size=plan.element_size,
        field_size=plan.field_size,
        raw_width=plan.raw_width,
    )
```

(c) Chained-dot: extend per what Step 1's verification showed — either a pure
displacement fold (no new arm needed beyond accepting
`MemberPlace(MemberPlace(VariablePlace))` in the dot arm with summed offsets,
mirroring how the IR builder/legacy emitter handles it today) or a nested plan
whose materialization is `inner_operand =
self._materialize_address_plan(plan.base)` →
`self._emit_resolved_load(inner_operand)` (decay lea) → `mov {bx}, {acc}` +
`ax_clear()` — the exact sequence of generator.py:4862-4866.

- [ ] **Step 4: Run tests + the gate bundle**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q && python3
tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py && python3
tests/test_cc_bits.py` Expected: all PASS, zero deltas.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py cc/codegen/x86/generator.py tests/unit/test_cc_address_plan.py
git commit -m "feat(cc): plan arrow and chained member bases for native loads (phase 1)"
```

---

### Task 5: native ir.Store for planned member shapes

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`ir.Store` case)
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing test**

```python
ARROW_STORE_SOURCE = """
struct node { int value; };
void write_value(struct node *n, int v) {
    n->value = v;
}
"""


def test_arrow_member_store_uses_native_plan_path():
    generator = _generate(ARROW_STORE_SOURCE)
    # The store's Address must have planned (not fallen back to legacy).
    assert len(generator._ir_address_plans) == 1
    assert len(generator._ir_address_ops) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q` Expected: FAIL —
Store consumers still read `_ir_address_ops` (the planner covers the shape, but
if Task 4 left store-consumed Addresses unplanned, adjust: the planner is
terminal-agnostic, so this test fails only on the `ir.Store` case still raising
KeyError or the plan dict not consulted; let the actual failure guide which).

- [ ] **Step 3: Implement the native Store case**

Mirror `_emit_member_scalar_resolved_store`'s four orderings
(generator.py:2337-2396) using the plan's `base_is_static` /
`base_preserves_accumulator` facts and the same shared sub-emitters:

```python
case ir.Store(address=address, value=value):
    plan = self._ir_address_plans.get(address)
    if plan is not None:
        value_node = self._ir_value_to_ast(value)
        self.ax_clear()
        if plan.base_is_static:
            operand = self._materialize_address_plan(plan)
            if operand.bitfield is not None:
                address_string = self._build_address(operand.base, operand.displacement)
                if operand.bitfield.bit_width == 1 and isinstance(value_node, Int) and value_node.value in (0, 1):
                    self._emit_bitfield_write_literal(operand.bitfield, addr=address_string, value=value_node.value)
                    return
                if self._try_fold_bitfield_int_store(operand, value_node):
                    return
            self.generate_expression(value_node)
            self._emit_resolved_field_store(operand, value_node)
        elif plan.base_preserves_accumulator:
            bitfield_literal = self._member_bitfield_literal_from_plan(plan, value_node)
            if bitfield_literal is not None:
                operand = self._materialize_address_plan(plan)
                address_string = self._build_address(operand.base, operand.displacement)
                self._emit_bitfield_write_literal(operand.bitfield, addr=address_string, value=bitfield_literal)
                return
            self.generate_expression(value_node)
            operand = self._materialize_address_plan(plan)
            self._emit_resolved_field_store(operand, value_node)
        else:
            operand = self._materialize_address_plan(plan)
            self.generate_expression(value_node)
            self._emit_resolved_field_store(operand, value_node)
        self.ax_clear()
    else:
        # <existing legacy body, unchanged>
```

Notes:
- `_member_bitfield_literal_from_plan` is `_member_bitfield_literal` (find it
  with awk) re-keyed on the plan's bitfield instead of re-deriving from the
  place — extract the pure literal-eligibility check the same
  split-don't-duplicate way.
- The trailing `self.ax_clear()` mirrors the legacy `ir.Store` case
  (emission.py:2078-2082) — keep its comment.
- The value is still a byte-safe leaf in phase 1 (`_is_byte_safe_store_rhs`
  unchanged), so `self._ir_value_to_ast(value)` round-trips exactly as the
  legacy path did. Widening admissible RHS classes is phases 2-3, NOT here.

- [ ] **Step 4: Run tests + the gate bundle**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q && python3
tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py && python3
tests/test_cc_bits.py` Expected: all PASS, zero deltas. (The bitfield matrix is
exercised by `python3 tests/test_cc_bitfields.py` — run it too.)

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py cc/codegen/x86/generator.py tests/unit/test_cc_address_plan.py
git commit -m "feat(cc): native AddressPlan store path for member shapes (phase 1)"
```

---

### Task 6: single-dynamic-index shapes (loads + stores)

`arr[index].member` (struct-array) and the single-subscript member-index shape.
The plan gains its first `terms` entry; the materializer gains the
scale-and-accumulate arm.

**Files:**
- Modify: `cc/codegen/x86/emission.py`, `cc/codegen/x86/generator.py` (split
  `_resolve_member_index` generator.py:4673 into pure layout + emitting halves)
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing test**

```python
STRUCT_ARRAY_SOURCE = """
struct entry { int key; int payload; };
struct entry table[8];
int read_payload(int index) {
    int out;
    out = table[index].payload;
    return out;
}
"""


def test_struct_array_member_load_plans_one_term():
    generator = _generate(STRUCT_ARRAY_SOURCE)
    plans = list(generator._ir_address_plans.values())
    assert len(plans) == 1
    plan = plans[0]
    assert len(plan.terms) == 1
    assert plan.terms[0].scale == 8  # sizeof(struct entry)
    assert plan.displacement == 4  # offset of payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q` Expected: FAIL
(shape falls through to legacy; plans dict empty).

- [ ] **Step 3: Implement**

Planner: the `ir.Address` for these shapes carries `indices=(value,)` (the IR
builder pre-lowered the index). Constant `Int` index values fold into
`displacement` (`index * scale`) with NO term; dynamic values become
`AddressTerm(index_value=value, scale=stride)`. Derive `stride` / member offset
from the pure half of the split `_resolve_member_index` / `_member_layout_on`
helpers (read both; extract pure halves as in Task 3).

Materializer: the dynamic-term arm must reproduce `_accumulate_subscript`
(generator.py:440-463) exactly, evaluating the index Value the same way the
legacy path's re-seated `Var`/`Int` node evaluated:

```python
    if plan.terms:
        operand = _operand_from_plan(base=plan.base, base_kind=plan.base_kind)
        bx_register = self.target.bx_register
        for term in plan.terms:
            if operand.index is not None:
                self.emit(f"        push {bx_register}")
            self.generate_expression(self._ir_value_to_ast(term.index_value))  # accumulator = index
            self._emit_scale_index(self.target.acc, scale=term.scale)  # accumulator = byte offset
            if operand.index is not None:
                self.emit(f"        pop {bx_register}")
                self.emit(f"        add {bx_register}, {self.target.acc}")
            else:
                self.emit(f"        mov {bx_register}, {self.target.acc}")
                operand.index = bx_register
        return operand
```

(`generate_expression(self._ir_value_to_ast(...))` is byte-identical to what the
legacy re-seat ran — the index Value round-trips to the same `Var`/`Int` leaf.
This is value loading, not shape re-seating; the same conversion every other IR
op case uses.)

Terminal wrappers: the struct-array load/store run through the protect-BX
terminals (`_emit_subscript_resolved_load` generator.py:3334,
`_emit_subscript_resolved_store` generator.py:3360). Mirror their structure in
the native cases for plans with terms:

- Load: `ax_clear` → `protect_bx = self._bx_holds_pinned_var()` → optional `push
  bx` → materialize → `lea`-decay or `_emit_field_load` → optional `pop bx` →
  `ax_clear` → store-to-local tail.
- Store: `allowed`-width check → `ax_clear` → optional `push bx` →
  `generate_expression(value_node)` → `push acc` → materialize → `pop acc` →
  `ax_clear` → `_emit_field_store` → optional `pop bx`.

These wrappers are short enough to inline in the native cases; quote the legacy
bodies above as the parity reference while writing them, and keep
`operand.field_size = operand.element_size` / `decay_to_address = False`
subscript finalization (generator.py:6563-6568) in the planner's subscript-shape
arm (a subscript selects one scalar element).

ORDERING NOTE: the member-index store has its own no-spill constant-index
ordering (`_emit_member_index_resolved_store`, generator.py:2312-2335);
constant-index plans (index folded, no terms) take the static-ordering arm,
dynamic-index plans the push-value-first arm — which the term/no-term split
gives naturally. Verify against the golden.

- [ ] **Step 4: Run tests + the gate bundle**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q && python3
tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py && python3
tests/test_cc_bits.py && python3 tests/test_cc_member_index_address.py`
Expected: all PASS, zero deltas.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py cc/codegen/x86/generator.py tests/unit/test_cc_address_plan.py
git commit -m "feat(cc): plan single-dynamic-index shapes with AddressTerm scale-and-accumulate (phase 1)"
```

---

### Task 7: multidim, mixed chains, and pointer-to-array (Horner plans)

`m[i][j]`, `g.cells[i][j]`, `table[i].name[j]`, `p[i][j]` (pointer-to-array),
and the `name[index]()` call slot. Multi-term plans whose legacy materialization
is the row-major Horner walk.

**Files:**
- Modify: `cc/codegen/x86/emission.py`, `cc/codegen/x86/generator.py` (split
  `_emit_multidim_subscript_address` generator.py:2503,
  `_emit_multidim_member_address` generator.py:2411,
  `_emit_pointer_to_array_address` generator.py:3012)
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing test**

```python
MULTIDIM_SOURCE = """
int m[4][3];
int read_cell(int i, int j) {
    int out;
    out = m[i][j];
    return out;
}
"""


def test_multidim_load_plans_horner_terms():
    generator = _generate(MULTIDIM_SOURCE)
    plans = list(generator._ir_address_plans.values())
    assert len(plans) == 1
    plan = plans[0]
    assert plan.horner is True
    assert [term.scale for term in plan.terms] == [12, 4]  # row stride, element
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q` Expected: FAIL.

- [ ] **Step 3: Implement**

Split each of the three legacy Horner emitters into a pure half (dimension
strides, base operand, member offset — feeding the planner) and an emitting half
(the Horner instruction walk — `multiply, add next index, scale`), exactly as
they are written today. The emitting half becomes ONE shared method consumed
both by the legacy `resolve_address` arms (until Task 9) and by the native
materializer for `plan.horner` plans:

```python
def _emit_horner_index_walk(self, *, index_nodes: list[Node], strides: list[int]) -> str:
    """Emit the row-major Horner accumulation; return the index register.

    The exact instruction walk the three multidim emitters shared — moved
    here verbatim from _emit_multidim_subscript_address so the legacy arms
    and the AddressPlan materializer run one body.
    """
```

(The exact signature must follow from reading the three emitters first — they
may differ in whether the member offset folds before or after the walk; preserve
each caller's current order. If the three walks are NOT identical, extract
per-caller emitting halves instead — split, don't unify-and-hope; unification
beyond what is byte-provable is 3c's job, not phase 1's.)

The native materializer's Horner arm converts term values via
`self._ir_value_to_ast` into the `index_nodes` list and calls the shared
emitting half — same bytes by construction.

The `IndirectCall` slot plan (`name[index]()`, single term, scale = pointer
size) materializes through the SAME single-term arm as Task 6; the existing
`call [table+register*scale]` fold lives in the function-pointer call terminal —
drive `generate_statement(PlaceCall(...))`'s underlying emitter from the
operand. Read how the legacy `ir.IndirectCall` case's re-seated `PlaceCall`
reaches `_emit_function_pointer_call` (emission.py:718) and pass the
materialized operand to the same helper. If `_emit_function_pointer_call` only
accepts AST, split it pure/emitting first (same pattern).

Also plan mixed chains (`table[i].name[j]`): the planner walks
MemberPlace/SubscriptPlace segments exactly as the IR builder's
`_lower_subscript_chain_indices` does (cc/ir.py), pairing each subscript with
its layout stride and summing member offsets into `displacement`.

- [ ] **Step 4: Run tests + the gate bundle + multidim suites**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py
tests/unit/test_cc_multidim_codegen_guard.py
tests/unit/test_cc_multidim_runtime.py
tests/unit/test_cc_multidim_struct_field_runtime.py
tests/unit/test_cc_pointer_to_array_runtime.py -q && python3
tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py && python3
tests/test_cc_bits.py && python3 tests/test_cc_fptr_array.py` Expected: all
PASS, zero deltas.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py cc/codegen/x86/generator.py tests/unit/test_cc_address_plan.py
git commit -m "feat(cc): Horner AddressPlans for multidim, mixed chains, and pointer-to-array (phase 1)"
```

---

### Task 8: native AddressOf, IncrementDecrement, and IndirectCall terminals

**Files:**
- Modify: `cc/codegen/x86/emission.py` (three terminal cases)
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing tests**

```python
ADDRESS_OF_SOURCE = """
struct node { int value; };
int *field_pointer(struct node *n) {
    int *p;
    p = &n->value;
    return p;
}
"""

INCREMENT_SOURCE = """
struct counter { int hits; };
void bump(struct counter *c) {
    c->hits++;
}
"""


def test_address_of_and_increment_use_native_plan_path():
    for source in (ADDRESS_OF_SOURCE, INCREMENT_SOURCE):
        generator = _generate(source)
        assert len(generator._ir_address_ops) == 0, source
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q` Expected: FAIL —
the three terminal cases still read `_ir_address_ops`.

- [ ] **Step 3: Implement**

- `ir.AddressOf`: mirror `_emit_place_address_of` (generator.py:2593) — split it
  pure/emitting; the emitting half takes the materialized operand and produces
  the `lea` / label-immediate accumulator value; then run
  `_store_accumulator_to_local(destination, direct_register=...)` exactly as the
  native Load does.
- `ir.IncrementDecrement`: the legacy path re-seated a `PlaceIncrementDecrement`
  statement. Find its `generate_statement` arm (awk for
  `PlaceIncrementDecrement` in generator.py), split the read-modify-write
  emitter so its core takes a `MemoryOperand` + delta + is_postfix, and call
  that core from the native case (the single-instruction memory `inc`/`dec` fold
  must survive — it keys on the operand, not the AST).
- `ir.IndirectCall`: completed in Task 7's slot work; this task wires the case
  and deletes its re-seat block.

Each native case follows the same shape:

```python
case ir.IncrementDecrement(address=address, delta=delta, is_postfix=is_postfix):
    plan = self._ir_address_plans.get(address)
    if plan is not None:
        operand = self._materialize_address_plan(plan)
        self._emit_resolved_increment_decrement(operand, delta=delta, is_postfix=is_postfix)
    else:
        # <existing legacy body, unchanged>
```

- [ ] **Step 4: Run tests + the gate bundle**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q && python3
tests/test_cc_function_sizes.py && python3 tests/test_cc_place.py && python3
tests/test_cc_bits.py` Expected: all PASS, zero deltas.

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py cc/codegen/x86/generator.py tests/unit/test_cc_address_plan.py
git commit -m "feat(cc): native AddressOf, IncrementDecrement, and IndirectCall terminals (phase 1)"
```

---

### Task 9: totality — delete the AST re-seat machinery

**Files:**
- Modify: `cc/codegen/x86/emission.py`
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing test**

```python
def test_planner_is_total_over_the_corpus():
    # Every ir.Address the corpus produces must plan; the legacy dict
    # must stay empty for every translation unit.
    import glob
    from pathlib import Path

    for pattern in ("user/libbboeos/*.c", "user/programs/*.c"):
        for path in sorted(glob.glob(str(REPO_ROOT / pattern))):
            generator = _generate(Path(path).read_text())
            assert generator._ir_address_ops == {}, path
```

(Reuse the corpus-compile pattern from `tests/test_cc_function_sizes.py` for
include paths / defines — the bare `_generate` needs the same
`_discover_include_paths` preprocessing the census script in the header uses. If
full-corpus compilation in a unit test is too slow, mark it with
`@pytest.mark.slow` and run it explicitly here.)

- [ ] **Step 2: Run test to verify current state**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q -k total`
Expected: PASS if Tasks 3-8 covered every folded shape; any FAIL lists the
translation unit whose shape still falls through — extend the planner for it
(same split-don't-duplicate pattern) before proceeding. Do not delete the legacy
path while this test fails.

- [ ] **Step 3: Delete the re-seat machinery**

Once total:
- `_plan_ir_address` raises on unplannable shapes instead of returning None:

```python
    message = f"unplannable ir.Address shape: {type(shape).__name__}"
    raise CompileError(message, line=shape.line)
```

- Delete `_ir_address_with_index` (emission.py:1830),
  `_reseat_nested_subscript_indices`, the `_ir_address_ops` dict and its resets,
  and every `else:` legacy branch added in Tasks 3-8 (the native body becomes
  the whole case).
- `_ir_value_to_ast` SURVIVES — it is the value-leaf converter every IR op case
  uses (BinaryOperation, Copy, Call args), not shape re-seat machinery.
- Update the `ir.py` docstrings that referenced the re-seat
  (`_is_byte_safe_store_rhs` keeps its ledger pointer; the emission-side
  comments referencing "re-seat" get rewritten to describe plans).

- [ ] **Step 4: Run the full gate bundle + unit suite**

Run: `python3 -m pytest tests/unit -q && python3 tests/test_cc_function_sizes.py
&& python3 tests/test_cc_place.py && python3 tests/test_cc_bits.py` Expected:
all PASS, zero deltas. (`tests/unit/test_add_file.py` has a known
concurrent-image-build flake — re-run if it alone fails.)

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py cc/ir.py tests/unit/test_cc_address_plan.py
git commit -m "refactor(cc): delete the AST re-seat machinery; AddressPlanner is total (phase 1)"
```

---

### Task 10: declare clobber facts (unconsumed)

**Files:**
- Modify: `cc/codegen/x86/emission.py` (`_plan_ir_address` sets `clobbers`)
- Test: `tests/unit/test_cc_address_plan.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_pure_displacement_plan_declares_no_clobbers():
    generator = _generate(DOT_MEMBER_SOURCE)
    plan = next(iter(generator._ir_address_plans.values()))
    assert plan.clobbers == frozenset()


def test_dynamic_term_plan_declares_scratch_clobbers():
    generator = _generate(STRUCT_ARRAY_SOURCE)
    plan = next(iter(generator._ir_address_plans.values()))
    assert plan.clobbers == frozenset({"ax", "bx"})


def test_pointer_base_plan_declares_base_register_clobber():
    generator = _generate(ARROW_MEMBER_SOURCE)
    plan = next(iter(generator._ir_address_plans.values()))
    # _load_member_base picks SI or BX; assert against what it actually picks
    # for this shape (read it; likely {"bx"} or {"si"}).
    assert plan.clobbers in (frozenset({"bx"}), frozenset({"si"}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q -k clobber`
Expected: FAIL — `clobbers` is always `frozenset()`.

- [ ] **Step 3: Implement**

The planner sets `clobbers` from the facts it already derived (canonical 16-bit
names, matching `BUILTIN_CLOBBERS` convention, generator.py:205): empty for
pure-displacement static-base plans; `{"ax", "bx"}` when any dynamic term scales
through the accumulator into BX; the base-load register for pointer bases; the
union for combined shapes; Horner plans declare what their walk touches (read
the emitting half). NO consumer is wired — phase 2 feeds these into
`regalloc_inputs`. Tighten the third test to the actual register once read.

- [ ] **Step 4: Run tests + the gate bundle**

Run: `python3 -m pytest tests/unit/test_cc_address_plan.py -q && python3
tests/test_cc_function_sizes.py` Expected: PASS, zero deltas (clobbers are data
only).

- [ ] **Step 5: Commit**

```bash
git add cc/codegen/x86/emission.py tests/unit/test_cc_address_plan.py
git commit -m "feat(cc): declare per-AddressPlan clobber facts (unconsumed until phase 2)"
```

---

### Task 11: full verification matrix and PR

- [ ] **Step 1: Run the full local matrix**

Per repo policy for compiler-architecture changes, run every suite the CI matrix
runs, locally:

```bash
python3 -m pytest tests/unit -q
python3 tests/test_cc_function_sizes.py
python3 tests/test_cc_place.py
python3 tests/test_cc_bits.py
python3 tests/test_cc_bitfields.py
python3 tests/test_cc_casts.py
python3 tests/test_cc_local_structs.py
python3 tests/test_cc_member_index_address.py
python3 tests/test_cc_fptr_array.py
python3 tests/test_cc_loop_induction.py
python3 tests/test_asm.py
python3 tests/test_programs.py
python3 tests/test_programs.py --filesystem ext2
```

(Check `.github/workflows/test.yml` for any suite added since this plan was
written and run those too.) Expected: every suite PASS; byte gate zero deltas.

- [ ] **Step 2: Documentation**

Update `docs/CHANGELOG.md` (Unreleased) with a one-line entry; run `python3
tools/wrap_md.py docs/CHANGELOG.md`.

- [ ] **Step 3: Push and open the PR**

```bash
git push origin bboe/cc-native-address-phase1
gh pr create --title "feat(cc): native-Address emission flip-over (phase 1)" --body "..."
```

PR body: link the design doc and the ledger on `design-specs`; state the 0-delta
result (361 functions / 49 files), the deleted re-seat inventory, and that
clobber facts are declared-unconsumed pending phase 2. Use `--merge` (never
squash) when merging after review.

---

## Self-review notes (already applied)

- Every task's gate bundle includes the byte gate — phase 1 has no legitimate
  size change; any `GREW`/`shrank` line means a parity bug, not a refresh
  candidate.
- Tasks 3-8 each leave the legacy path intact behind a None-plan fallback, so
  any single task can be reverted without breaking the others.
- The split-don't-duplicate strategy is stated once in the header and reinforced
  where each mixed helper is named with its line number.
- Where this plan could not transcribe legacy code it has not quoted
  (`_emit_multidim_*`, `_emit_place_address_of`, the `PlaceIncrementDecrement`
  arm), the task instructs an in-place pure/emitting split of the original — the
  original body keeps emitting the bytes, so no transcription fidelity risk
  exists.

## Phase-1 errata (2026-06-07, implementation `bboe/cc-native-address-phase1`)

Task 9's "delete the AST re-seat machinery" proved impossible at 0-delta within
phase 1: the array-of-pointers subscript chain (`name[i][j]` over `char
*name[N]`, 6 corpus sites in shell.c) requires a mid-chain element-pointer load
that has no plan model without an IR-visible type table, and four
diagnostic-owning arms (aliased-pointer and non-pointer-holder deref stores,
bitfield AddressOf/IncrementDecrement, undefined-name call slots) keep their
place-anchored CompileErrors on the legacy arms. `_ir_address_with_index` /
`_reseat_nested_subscript_indices` therefore survive as a NARROWED residual
path, locked by
`tests/unit/test_cc_address_plan.py::test_residual_address_census_matches_allowlist`
(`RESIDUAL_CENSUS_ALLOWLIST = {"user/programs/shell.c": 6}`). The deletion moves
to phase 4 (3b.2), which adds the chain-splitting element-pointer-load plan
extension. A controller-added slice (Task 9a) planned bare deref stores
`*pointer = leaf` natively to narrow the census to the array-of-pointers family
alone.
