# Register Allocator — PR 1 (Unwired Engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure, fully-unit-tested register-allocation engine — IR-level liveness/interference over the CFG plus a cost-aware Chaitin-Briggs graph-coloring allocator — in a new module `cc/regalloc.py`, with **no codegen wiring** (nothing imports it yet, so the build is byte-neutral by construction).

**Architecture:** One self-contained module `cc/regalloc.py` (matching cc.py's single-file analysis-module style — `cc/ssa.py`, `cc/cfg.py`, `cc/loops.py`). It exposes (1) `build_interference(function, *, allocatable)` — IR-level backward-dataflow liveness over `cc.cfg.build_cfg`, producing an interference graph, move-pair set, and per-value live-across-call counts; and (2) `color(interference, moves, constraints, costs)` — a pure Chaitin-Briggs allocator (conservative coalescing → simplify → optimistic spill → select with soft-cost register choice) returning value→register homes + a spilled set; and (3) a `allocate(...)` convenience wiring the two. The **target-specific** constraint/cost inputs (the register pool, byte-alias/16-bit-index legality, regparm precolors, clobber-cost economics) are **parameters supplied by the caller** — PR 1 keeps the engine pure and feeds it synthetic inputs from unit tests; PR 2/3 will compute the real inputs from the generator's `register_clobber_counts` / `_builtin_clobbers` / `Target` data.

**Tech Stack:** Python 3, `cc.ir` / `cc.cfg` / `cc.ast_nodes`, `pytest` (unit suite under `tests/unit/`, run via `python3 -m pytest tests/unit/`).

**Out of scope for PR 1:** any change to `cc/codegen/`, emission, or the existing auto-pin. No file outside `cc/regalloc.py` and `tests/unit/test_cc_regalloc.py` is touched. (Wiring locals onto the engine is PR 2; IR temps PR 3; cleanup PR 4 — see the design doc.)

---

## File Structure

- **Create `cc/regalloc.py`** — the entire engine: public dataclasses (`RegisterConstraints`, `CostModel`, `Allocation`, `InterferenceResult`), the `RegallocError` exception, the def/use helpers, `build_interference`, `color`, and `allocate`.
- **Create `tests/unit/test_cc_regalloc.py`** — pure unit tests building synthetic `list[ir.Instruction]` bodies and synthetic constraint/cost inputs by hand (mirroring `tests/unit/test_cc_cfg.py`).

No other files change. The module is imported by nobody in PR 1.

---

## Conventions used throughout this plan

- A **value** is a `str` naming an allocatable quantity (a user local/param or an `_ir_*` temp). Globals, labels (start with `.`), and builtin/function names are **not** values.
- A **register** is a `str` from the target pool (e.g. `"ebx"`, `"ecx"`). PR 1 never hardcodes a pool — the caller passes `RegisterConstraints.pool`.
- `SPILL` is represented by membership in `Allocation.spilled` (no register assigned).
- Run unit tests with: `python3 -m pytest tests/unit/test_cc_regalloc.py -v` (from `/home/ubuntu/bboeos`).
- The implementer works in a worktree off `main` (created via `superpowers:using-git-worktrees` at execution time). All commit messages end with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: Module skeleton + public types

**Files:**
- Create: `cc/regalloc.py`
- Test: `tests/unit/test_cc_regalloc.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cc_regalloc.py
"""Tests for cc.regalloc — IR-level liveness/interference + Chaitin-Briggs coloring.

Each test builds a small flat IR by hand (avoiding the AST + Builder roundtrip)
and synthetic constraint/cost inputs, so the expected allocation is obvious from
the test source.  The engine is pure: no codegen, no target import.
"""

from __future__ import annotations

from cc import ast_nodes, ir
from cc.regalloc import (
    Allocation,
    CostModel,
    InterferenceResult,
    RegallocError,
    RegisterConstraints,
    allocate,
    build_interference,
    color,
)


def _function(body: list[ir.Instruction], /) -> ir.Function:
    """Wrap *body* in a minimal ir.Function for analysis."""
    ast = ast_nodes.Function(body=[], line=1, name="f", params=[])
    return ir.Function(ast_node=ast, body=body, strings=[])


def test_public_types_construct() -> None:
    """The public dataclasses construct with their documented fields."""
    constraints = RegisterConstraints(pool=("ebx", "ecx"), allowed={}, precolored={})
    costs = CostModel(spill_benefit={}, register_save_cost={})
    allocation = Allocation(homes={"x": "ebx"}, spilled=frozenset({"y"}))
    assert constraints.pool == ("ebx", "ecx")
    assert costs.spill_benefit == {}
    assert allocation.homes["x"] == "ebx"
    assert "y" in allocation.spilled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py::test_public_types_construct -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cc.regalloc'`.

- [ ] **Step 3: Write minimal implementation**

```python
# cc/regalloc.py
"""Unified graph-coloring register allocator over the flat IR + CFG.

PR 1 (this module) is the *unwired engine*: pure liveness/interference and a
cost-aware Chaitin-Briggs colorer.  Nothing in cc/codegen imports it yet, so its
introduction is byte-neutral.  Target-specific inputs — the register pool,
byte-alias / 16-bit-index legality, regparm precolors, and the call-clobber cost
economics — are passed in by the caller (see RegisterConstraints / CostModel);
PR 2/3 compute them from the generator's clobber data and wire emission to the
result.

Public API:

    inter = build_interference(function, allocatable=frozenset({"x", "_ir_0"}))
    alloc = color(inter.graph, inter.moves, constraints, costs)
    # or, end to end:
    alloc = allocate(function, allocatable=..., constraints=..., costs=...)

``alloc.homes`` maps value -> register; ``alloc.spilled`` is the set of values
left in memory.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Iterator

from cc import ast_nodes, cfg, ir


class RegallocError(Exception):
    """Raised when the allocator meets an IR shape it does not model.

    Mirrors ``cc.codegen.liveness.LivenessAnalysisError``: failing loud forces
    the def/use model to be updated when a new IR shape lands, rather than
    silently understating interference (which would let two simultaneously-live
    values share a register — a miscompile).
    """


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class RegisterConstraints:
    """Hard register constraints the colorer must respect.

    ``pool`` is the ordered tuple of allocatable physical registers (``K`` =
    ``len(pool)``).  ``allowed`` maps a value to the subset of ``pool`` it may
    occupy (a value absent from ``allowed`` may use any pool register); this is
    where byte-alias and 16-bit-index legality land.  ``precolored`` pins a
    value to a fixed register (e.g. a regparm parameter that arrives in EAX/
    EDX/ECX); precolored values are never simplified or spilled.
    """

    pool: tuple[str, ...]
    allowed: dict[str, frozenset[str]]
    precolored: dict[str, str]


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class CostModel:
    """The soft-cost economics driving register choice and spill decisions.

    ``spill_benefit`` maps a value to how much it wants a register (the auto-pin
    reference count); a value whose benefit does not exceed its chosen
    register's save cost is spilled instead.  ``register_save_cost`` maps a
    value to ``{register: push/pop save cost}`` — the per-call-crossing cost of
    homing that value in that register (from ``register_clobber_counts`` minus
    pre-first-store elision in PR 2/3).  A missing entry means zero cost.
    """

    spill_benefit: dict[str, int]
    register_save_cost: dict[str, dict[str, int]]


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Allocation:
    """The colorer's result: register homes + the spilled set."""

    homes: dict[str, str]
    spilled: frozenset[str]


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class InterferenceResult:
    """Output of ``build_interference``.

    ``graph`` is the symmetric adjacency map over allocatable values.  ``moves``
    is the set of coalesce candidates (each a frozenset of two values related by
    a ``Copy``).  ``live_across_call`` maps a value to the number of ``Call`` /
    ``TailCall`` / ``CarryBranch`` instructions it is live across (feeds the
    save-cost model in PR 2/3).
    """

    graph: dict[str, set[str]]
    moves: set[frozenset[str]]
    live_across_call: dict[str, int]
```

(`build_interference`, `color`, and `allocate` are added in later tasks; importing their names will fail until then — that is expected and Task 2/4/8 add them. To keep this task's test green now, add the three names as stubs that raise, so the import in Step 1 resolves:)

```python
def build_interference(function: ir.Function, /, *, allocatable: frozenset[str]) -> InterferenceResult:
    """Stubbed in Task 1; implemented in Task 3."""
    raise NotImplementedError


def color(
    interference: dict[str, set[str]],
    moves: set[frozenset[str]],
    constraints: RegisterConstraints,
    costs: CostModel,
    /,
) -> Allocation:
    """Stubbed in Task 1; implemented in Tasks 4-7."""
    raise NotImplementedError


def allocate(
    function: ir.Function,
    /,
    *,
    allocatable: frozenset[str],
    constraints: RegisterConstraints,
    costs: CostModel,
) -> Allocation:
    """Stubbed in Task 1; implemented in Task 8."""
    raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py::test_public_types_construct -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cc/regalloc.py tests/unit/test_cc_regalloc.py
git commit -m "feat(regalloc): module skeleton + public types

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: IR def/use extraction

**Files:**
- Modify: `cc/regalloc.py`
- Test: `tests/unit/test_cc_regalloc.py`

Defs come only from `BinaryOperation`/`Copy`/`Index`/`Call` destinations (mirror `cc.ir_optimize._instruction_destination`). Uses must be **exhaustive** — VALUE_FIELDS reads, the name-string operands the VALUE_FIELDS walk skips (`Index.base`, `IndexAssign.base`, `RepString.dest`/`source`), and opaque-AST reads (`Access`/`Block`/`CarryBranch`/`Switch`) via a local `_iter_ast_var_names` copy (the same duplication `cc/ssa.py` uses). An unmodeled instruction raises `RegallocError`.

- [ ] **Step 1: Write the failing test**

```python
def test_defs_and_uses_cover_value_fields_and_base_names() -> None:
    """Defs = destination; uses = value-field reads + Index/IndexAssign base names."""
    from cc.regalloc import _instruction_defs, _instruction_uses

    binop = ir.BinaryOperation(destination="_ir_0", left="a", operation="+", right=2)
    assert _instruction_defs(binop) == ("_ir_0",)
    assert set(_instruction_uses(binop)) == {"a"}  # the literal 2 is not a name

    index = ir.Index(base="arr", destination="_ir_1", index="i")
    assert _instruction_defs(index) == ("_ir_1",)
    assert set(_instruction_uses(index)) == {"arr", "i"}  # base name included

    store = ir.IndexAssign(base="arr", index="j", source="v")
    assert _instruction_defs(store) == ()  # IndexAssign defines nothing
    assert set(_instruction_uses(store)) == {"arr", "j", "v"}

    call = ir.Call(args=("x", 3), destination="_ir_2", name="f")
    assert _instruction_defs(call) == ("_ir_2",)
    assert set(_instruction_uses(call)) == {"x"}


def test_uses_walk_opaque_block_ast() -> None:
    """Block/Access reads are discovered by walking the wrapped AST for Var names."""
    from cc.regalloc import _instruction_uses

    node = ast_nodes.Assign(line=1, name="_ir_3", expr=ast_nodes.Var(line=1, name="k"))
    block = ir.Block(node=node)
    assert "k" in set(_instruction_uses(block))


def test_unmodeled_instruction_raises() -> None:
    """An IR shape with no def/use rule raises RegallocError (fail loud)."""
    import pytest

    from cc.regalloc import _instruction_uses

    class _Bogus:
        VALUE_FIELDS = ()

    with pytest.raises(RegallocError):
        list(_instruction_uses(_Bogus()))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "defs_and_uses or opaque or unmodeled" -v`
Expected: FAIL (`cannot import name '_instruction_defs'`).

- [ ] **Step 3: Write minimal implementation**

Add to `cc/regalloc.py`:

```python
#: Instruction classes that define a destination name.
_DESTINATION_TYPES = (ir.BinaryOperation, ir.Copy, ir.Index, ir.Call)

#: Instruction classes whose reads are fully covered by VALUE_FIELDS + the
#: explicit name-string fields handled in ``_instruction_uses``.
_MODELED_VALUE_TYPES = (
    ir.BinaryOperation,
    ir.BranchFalse,
    ir.Call,
    ir.Copy,
    ir.Index,
    ir.IndexAssign,
    ir.RepString,
    ir.Return,
    ir.TailCall,
)

#: Instruction classes that carry no reads and no allocatable defs.
_INERT_TYPES = (ir.Jump, ir.Label, ir.LoopBoundary, ir.InlineAsm)

#: Opaque AST-wrapping instructions whose reads are found by walking the AST.
_OPAQUE_TYPES = (ir.Access, ir.Block, ir.CarryBranch, ir.Switch)


def _iter_ast_var_names(node: object, /) -> Iterator[str]:
    """Yield every ``Var`` / ``VariablePlace`` name in the AST subtree at *node*.

    Local copy of ``cc.ir_optimize._iter_ast_var_names`` (cc/ssa.py keeps its
    own copy too) so regalloc.py does not import the optimizer's private API.
    """
    if isinstance(node, ast_nodes.Var):
        yield node.name
        return
    if isinstance(node, ast_nodes.VariablePlace):
        yield node.name
        return
    for bare_name_field in ("target_name", "object_name"):
        bare_name = getattr(node, bare_name_field, None)
        if isinstance(bare_name, str):
            yield bare_name
    if dataclasses.is_dataclass(node):
        for declared_field in dataclasses.fields(node):
            yield from _iter_ast_var_names(getattr(node, declared_field.name))
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_ast_var_names(item)


def _instruction_defs(instruction: ir.Instruction, /) -> tuple[str, ...]:
    """Return the destination name(s) written by *instruction* (empty if none)."""
    if isinstance(instruction, _DESTINATION_TYPES):
        destination = instruction.destination
        return () if destination is None else (destination,)
    return ()


def _instruction_uses(instruction: ir.Instruction, /) -> tuple[str, ...]:
    """Return every name read by *instruction*, exhaustively.

    Combines VALUE_FIELDS reads (filtered to ``str`` operands), the name-string
    operands the VALUE_FIELDS walk skips (``Index.base`` / ``IndexAssign.base`` /
    ``RepString.dest`` / ``RepString.source``), and opaque-AST reads.  Raises
    ``RegallocError`` for an unmodeled instruction so coverage stays exhaustive.
    """
    if isinstance(instruction, _INERT_TYPES):
        return ()
    if isinstance(instruction, _OPAQUE_TYPES):
        if isinstance(instruction, ir.Switch):
            names = list(_iter_ast_var_names(instruction.discriminant))
            for case in instruction.cases:
                for inner in case.body:
                    names.extend(_instruction_uses(inner))
            return tuple(names)
        ast_node = instruction.call_ast if isinstance(instruction, ir.CarryBranch) else instruction.node
        return tuple(_iter_ast_var_names(ast_node))
    if not isinstance(instruction, _MODELED_VALUE_TYPES):
        message = f"regalloc: unhandled instruction {type(instruction).__name__}"
        raise RegallocError(message)
    names: list[str] = []
    for field_name in instruction.VALUE_FIELDS:
        value = getattr(instruction, field_name)
        if value is None:
            continue
        operands = value if isinstance(value, tuple) else (value,)
        names.extend(operand for operand in operands if isinstance(operand, str))
    if isinstance(instruction, (ir.Index, ir.IndexAssign)):
        names.append(instruction.base)
    if isinstance(instruction, ir.RepString):
        names.append(instruction.dest)
        if instruction.source is not None:
            names.append(instruction.source)
    return tuple(names)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "defs_and_uses or opaque or unmodeled" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cc/regalloc.py tests/unit/test_cc_regalloc.py
git commit -m "feat(regalloc): exhaustive IR def/use extraction

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: IR-level liveness + interference builder

**Files:**
- Modify: `cc/regalloc.py`
- Test: `tests/unit/test_cc_regalloc.py`

Block-level backward dataflow over `cfg.build_cfg(function.body)`: compute per-block `use`/`def` (a use is a name read before being defined in the block; a def is any destination), then iterate `live_out = ∪ succ.live_in`, `live_in = use ∪ (live_out − def)` to a fixed point. Then walk each block **backward** from `live_out`, and at each instruction add interference edges between each def and every value in the current live set; record `Copy` def↔source pairs as moves; count `live_across_call`. Only **allocatable** names become graph nodes / move endpoints (filter via the `allocatable` set). The block's terminator participates as the last instruction.

- [ ] **Step 1: Write the failing test**

```python
def test_interference_two_simultaneously_live_values() -> None:
    """Two values both live at a point interfere; a dead-then-reused value does not."""
    # a = 1; b = 2; c = a + b; return c   -> a,b live together; c separate
    body = [
        ir.Copy(destination="a", source=1),
        ir.Copy(destination="b", source=2),
        ir.BinaryOperation(destination="c", left="a", operation="+", right="b"),
        ir.Return(value="c"),
    ]
    allocatable = frozenset({"a", "b", "c"})
    result = build_interference(_function(body), allocatable=allocatable)
    assert "b" in result.graph["a"] and "a" in result.graph["b"]
    # c is defined after a and b are dead (their last use is the BinaryOperation),
    # so c interferes with neither.
    assert result.graph.get("c", set()) == set()


def test_interference_move_pair_recorded() -> None:
    """A Copy between two allocatable values is a coalesce candidate."""
    body = [
        ir.Copy(destination="a", source=1),
        ir.Copy(destination="b", source="a"),
        ir.Return(value="b"),
    ]
    result = build_interference(_function(body), allocatable=frozenset({"a", "b"}))
    assert frozenset({"a", "b"}) in result.moves


def test_live_across_call_counted() -> None:
    """A value live across a Call is counted for save-cost."""
    body = [
        ir.Copy(destination="keep", source=1),
        ir.Call(args=(), destination="_ir_0", name="f"),
        ir.Return(value="keep"),
    ]
    result = build_interference(_function(body), allocatable=frozenset({"keep", "_ir_0"}))
    assert result.live_across_call.get("keep", 0) == 1


def test_non_allocatable_names_absent_from_graph() -> None:
    """Globals / labels are not allocatable and never become graph nodes."""
    body = [
        ir.Index(base="g_global", destination="_ir_0", index="i"),
        ir.Return(value="_ir_0"),
    ]
    result = build_interference(_function(body), allocatable=frozenset({"_ir_0", "i"}))
    assert "g_global" not in result.graph
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "interference or live_across or non_allocatable" -v`
Expected: FAIL (`build_interference` raises `NotImplementedError`).

- [ ] **Step 3: Write minimal implementation**

Replace the `build_interference` stub in `cc/regalloc.py` with:

```python
_CALL_TYPES = (ir.Call, ir.TailCall, ir.CarryBranch)


def _block_instructions(block: cfg.BasicBlock, /) -> list[ir.Instruction]:
    """Return a block's instructions followed by its terminator (if any)."""
    if block.terminator is None:
        return list(block.instructions)
    return [*block.instructions, block.terminator]


def build_interference(function: ir.Function, /, *, allocatable: frozenset[str]) -> InterferenceResult:
    """Compute the interference graph, move pairs, and live-across-call counts.

    Block-level backward dataflow to a fixed point, then a backward walk of each
    block adding Chaitin interference edges (each def vs. the live set) and
    recording Copy move pairs.  Only names in *allocatable* become graph nodes.
    """
    graph = cfg.build_cfg(function.body)
    blocks = graph.blocks

    block_use: dict[cfg.BasicBlock, set[str]] = {}
    block_def: dict[cfg.BasicBlock, set[str]] = {}
    for block in blocks:
        uses: set[str] = set()
        defs: set[str] = set()
        for instruction in _block_instructions(block):
            for name in _instruction_uses(instruction):
                if name in allocatable and name not in defs:
                    uses.add(name)
            for name in _instruction_defs(instruction):
                if name in allocatable:
                    defs.add(name)
        block_use[block] = uses
        block_def[block] = defs

    live_in: dict[cfg.BasicBlock, set[str]] = {block: set() for block in blocks}
    live_out: dict[cfg.BasicBlock, set[str]] = {block: set() for block in blocks}
    changed = True
    while changed:
        changed = False
        for block in blocks:
            new_out: set[str] = set()
            for successor in block.successors:
                new_out |= live_in[successor]
            new_in = block_use[block] | (new_out - block_def[block])
            if new_in != live_in[block] or new_out != live_out[block]:
                live_in[block] = new_in
                live_out[block] = new_out
                changed = True

    adjacency: dict[str, set[str]] = {name: set() for name in allocatable}
    moves: set[frozenset[str]] = set()
    live_across_call: Counter[str] = Counter()

    def add_edge(name_a: str, name_b: str) -> None:
        if name_a == name_b:
            return
        adjacency[name_a].add(name_b)
        adjacency[name_b].add(name_a)

    for block in blocks:
        live = set(live_out[block])
        for instruction in reversed(_block_instructions(block)):
            if isinstance(instruction, _CALL_TYPES):
                for name in live:
                    live_across_call[name] += 1
            defs = [name for name in _instruction_defs(instruction) if name in allocatable]
            for defined in defs:
                for other in live:
                    add_edge(defined, other)
            if isinstance(instruction, ir.Copy) and isinstance(instruction.source, str):
                if instruction.destination in allocatable and instruction.source in allocatable:
                    moves.add(frozenset({instruction.destination, instruction.source}))
            for defined in defs:
                live.discard(defined)
            for name in _instruction_uses(instruction):
                if name in allocatable:
                    live.add(name)

    return InterferenceResult(graph=adjacency, moves=moves, live_across_call=dict(live_across_call))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "interference or live_across or non_allocatable" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cc/regalloc.py tests/unit/test_cc_regalloc.py
git commit -m "feat(regalloc): IR-level liveness + interference builder

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Coloring core — simplify/select for a colorable graph

**Files:**
- Modify: `cc/regalloc.py`
- Test: `tests/unit/test_cc_regalloc.py`

Implement `color` for graphs that fit in `K = len(pool)` registers, with **no spilling yet** (Task 5 adds spill). Algorithm: precolored nodes are assigned up front and excluded from the worklist; repeatedly remove a non-precolored node of degree `< K` from the graph and push it on a stack; when empty, pop each node and assign it any register in its `allowed` set (default = full pool) not used by an already-colored neighbor. Respect `precolored` neighbors when choosing colors.

- [ ] **Step 1: Write the failing test**

```python
def _no_costs() -> CostModel:
    return CostModel(spill_benefit={}, register_save_cost={})


def test_color_triangle_three_registers() -> None:
    """A 3-clique colors with 3 registers, all distinct."""
    graph = {"a": {"b", "c"}, "b": {"a", "c"}, "c": {"a", "b"}}
    constraints = RegisterConstraints(pool=("ebx", "ecx", "edx"), allowed={}, precolored={})
    alloc = color(graph, set(), constraints, _no_costs())
    assert alloc.spilled == frozenset()
    homes = alloc.homes
    assert homes["a"] != homes["b"] != homes["c"] != homes["a"]
    assert set(homes.values()) <= {"ebx", "ecx", "edx"}


def test_color_respects_allowed_set() -> None:
    """A value restricted to one register lands there."""
    graph = {"x": set(), "y": set()}
    constraints = RegisterConstraints(
        pool=("ebx", "ecx"), allowed={"x": frozenset({"ecx"})}, precolored={}
    )
    alloc = color(graph, set(), constraints, _no_costs())
    assert alloc.homes["x"] == "ecx"


def test_color_respects_precolored_neighbor() -> None:
    """A node adjacent to a precolored register avoids that register."""
    graph = {"p": {"q"}, "q": {"p"}}
    constraints = RegisterConstraints(
        pool=("ebx", "ecx"), allowed={}, precolored={"p": "ebx"}
    )
    alloc = color(graph, set(), constraints, _no_costs())
    assert alloc.homes["p"] == "ebx"
    assert alloc.homes["q"] == "ecx"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "color_triangle or allowed_set or precolored_neighbor" -v`
Expected: FAIL (`color` raises `NotImplementedError`).

- [ ] **Step 3: Write minimal implementation**

Replace the `color` stub. (This task implements the no-spill path; Task 5 generalizes the marked branch.)

```python
def _allowed_registers(value: str, constraints: RegisterConstraints, /) -> tuple[str, ...]:
    """Return the pool registers *value* may occupy (full pool if unconstrained)."""
    permitted = constraints.allowed.get(value)
    if permitted is None:
        return constraints.pool
    return tuple(register for register in constraints.pool if register in permitted)


def color(
    interference: dict[str, set[str]],
    moves: set[frozenset[str]],
    constraints: RegisterConstraints,
    costs: CostModel,
    /,
) -> Allocation:
    """Color *interference* with the pool in *constraints*, spilling by cost.

    Chaitin-Briggs: (conservative coalescing — Task 7), simplify (remove
    ``< K`` degree non-precolored nodes), optimistic-spill push, then select
    (assign each popped node a legal color or actually spill).  *moves* and
    *costs* are unused until Tasks 5-7.
    """
    pool_size = len(constraints.pool)
    precolored = dict(constraints.precolored)
    # Working copy of the graph restricted to non-precolored nodes; precolored
    # nodes stay as fixed neighbors consulted during select.
    nodes = [name for name in interference if name not in precolored]
    degree: dict[str, set[str]] = {name: set(interference[name]) for name in nodes}

    stack: list[str] = []
    remaining = set(nodes)
    while remaining:
        simplifiable = sorted(
            name for name in remaining if len(degree[name] & remaining) < pool_size
        )
        if not simplifiable:
            # Optimistic spill (Task 5 chooses by cost); for the colorable
            # graphs in this task there is always a simplifiable node.
            simplifiable = [sorted(remaining)[0]]
        for name in simplifiable:
            if name in remaining:
                stack.append(name)
                remaining.discard(name)

    homes: dict[str, str] = dict(precolored)
    spilled: set[str] = set()
    while stack:
        name = stack.pop()
        used = {homes[neighbor] for neighbor in interference[name] if neighbor in homes}
        choice = next((reg for reg in _allowed_registers(name, constraints) if reg not in used), None)
        if choice is None:
            spilled.add(name)
        else:
            homes[name] = choice

    for name in precolored:
        homes.setdefault(name, precolored[name])
    return Allocation(homes={k: v for k, v in homes.items() if k not in spilled}, spilled=frozenset(spilled))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "color_triangle or allowed_set or precolored_neighbor" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cc/regalloc.py tests/unit/test_cc_regalloc.py
git commit -m "feat(regalloc): Chaitin simplify/select for colorable graphs

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Spilling by cost under register pressure

**Files:**
- Modify: `cc/regalloc.py`
- Test: `tests/unit/test_cc_regalloc.py`

When no node has degree `< K`, pick the **lowest-benefit** node to optimistically push (Briggs: it may still color on select). On select, a node that cannot be colored truly spills. Tie-break the spill choice by `spill_benefit / degree` (Chaitin's metric); lower benefit and higher degree → spill first. Reuse `CostModel.spill_benefit` (default 0 for missing entries).

- [ ] **Step 1: Write the failing test**

```python
def test_spill_lowest_benefit_under_pressure() -> None:
    """A 3-clique with only 2 registers spills the lowest-benefit node."""
    graph = {"a": {"b", "c"}, "b": {"a", "c"}, "c": {"a", "b"}}
    constraints = RegisterConstraints(pool=("ebx", "ecx"), allowed={}, precolored={})
    costs = CostModel(spill_benefit={"a": 10, "b": 10, "c": 1}, register_save_cost={})
    alloc = color(graph, set(), constraints, costs)
    assert alloc.spilled == frozenset({"c"})
    assert alloc.homes["a"] != alloc.homes["b"]


def test_no_spill_when_pressure_fits() -> None:
    """A 2-clique with 2 registers spills nothing even at equal benefit."""
    graph = {"a": {"b"}, "b": {"a"}}
    constraints = RegisterConstraints(pool=("ebx", "ecx"), allowed={}, precolored={})
    alloc = color(graph, set(), constraints, CostModel(spill_benefit={}, register_save_cost={}))
    assert alloc.spilled == frozenset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "spill_lowest or no_spill_when" -v`
Expected: FAIL (the Task 4 placeholder spill picks `sorted(remaining)[0]` = `"a"`, not the lowest-benefit `"c"`).

- [ ] **Step 3: Write minimal implementation**

Replace the simplify loop's optimistic-spill branch in `color`:

```python
    stack: list[str] = []
    remaining = set(nodes)
    while remaining:
        simplifiable = sorted(
            name for name in remaining if len(degree[name] & remaining) < pool_size
        )
        if simplifiable:
            for name in simplifiable:
                stack.append(name)
                remaining.discard(name)
            continue
        # No low-degree node: optimistically push the weakest spill candidate
        # (lowest benefit per current degree; Chaitin's spill metric).  It may
        # still receive a color on select (Briggs optimism).
        def _spill_metric(name: str) -> tuple[float, str]:
            current_degree = len(degree[name] & remaining) or 1
            benefit = costs.spill_benefit.get(name, 0)
            return (benefit / current_degree, name)

        victim = min(remaining, key=_spill_metric)
        stack.append(victim)
        remaining.discard(victim)
```

(The select loop from Task 4 already spills a node that cannot be colored, so no select change is needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "spill_lowest or no_spill_when" -v`
Expected: PASS. Also re-run Task 4's tests to confirm no regression:
Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "color_triangle or allowed_set or precolored_neighbor" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cc/regalloc.py tests/unit/test_cc_regalloc.py
git commit -m "feat(regalloc): optimistic spill by Chaitin cost metric

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Soft-cost register selection + benefit gate

**Files:**
- Modify: `cc/regalloc.py`
- Test: `tests/unit/test_cc_regalloc.py`

Two refinements on select: (1) among legal registers, choose the one with the **lowest `register_save_cost[value][reg]`** (the auto-pin economics — prefer the register this value crosses the fewest clobbering calls in); (2) the **benefit gate** — if even the cheapest legal register's save cost is `>= spill_benefit[value]`, spilling is no worse than homing, so spill instead (reproduces auto-pin's `refs > effective_cost` rule).

- [ ] **Step 1: Write the failing test**

```python
def test_select_prefers_cheapest_save_cost() -> None:
    """Among free registers, pick the one with the lowest save cost."""
    graph = {"x": set()}
    constraints = RegisterConstraints(pool=("ebx", "ecx"), allowed={}, precolored={})
    costs = CostModel(
        spill_benefit={"x": 100},
        register_save_cost={"x": {"ebx": 5, "ecx": 0}},
    )
    alloc = color(graph, set(), constraints, costs)
    assert alloc.homes["x"] == "ecx"


def test_benefit_gate_spills_when_save_cost_too_high() -> None:
    """A value whose benefit does not exceed its cheapest save cost is spilled."""
    graph = {"x": set()}
    constraints = RegisterConstraints(pool=("ebx",), allowed={}, precolored={})
    costs = CostModel(spill_benefit={"x": 2}, register_save_cost={"x": {"ebx": 2}})
    alloc = color(graph, set(), constraints, costs)
    assert "x" in alloc.spilled
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "cheapest_save or benefit_gate" -v`
Expected: FAIL (select currently picks the first free register and never applies the gate).

- [ ] **Step 3: Write minimal implementation**

Replace the select loop in `color`:

```python
    homes: dict[str, str] = dict(precolored)
    spilled: set[str] = set()
    while stack:
        name = stack.pop()
        used = {homes[neighbor] for neighbor in interference[name] if neighbor in homes}
        legal = [reg for reg in _allowed_registers(name, constraints) if reg not in used]
        if not legal:
            spilled.add(name)
            continue
        save_costs = costs.register_save_cost.get(name, {})
        choice = min(legal, key=lambda reg: (save_costs.get(reg, 0), constraints.pool.index(reg)))
        benefit = costs.spill_benefit.get(name, 0)
        if name in costs.spill_benefit and benefit <= save_costs.get(choice, 0):
            # Homing this value costs at least as much (in push/pop saves) as it
            # is worth — spill instead, mirroring auto-pin's refs > cost gate.
            spilled.add(name)
            continue
        homes[name] = choice
```

Note: the gate only fires when `name` has an explicit `spill_benefit` entry, so cost-free synthetic tests (and precolored nodes) are unaffected.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "cheapest_save or benefit_gate" -v`
Expected: PASS. Re-run Tasks 4-5 tests for no regression:
Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add cc/regalloc.py tests/unit/test_cc_regalloc.py
git commit -m "feat(regalloc): soft-cost register selection + benefit-gate spill

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Conservative coalescing of move pairs

**Files:**
- Modify: `cc/regalloc.py`
- Test: `tests/unit/test_cc_regalloc.py`

Coalesce move-related, **non-interfering** value pairs so a `Copy(a, b)` can be eliminated (both ends share a register). Use **Briggs conservative coalescing**: coalesce `{a, b}` only if the merged node has fewer than `K` neighbors of significant degree (`>= K`) — this guarantees coalescing never turns a colorable graph uncolorable. Implement as a pre-pass that picks safe move pairs, merges endpoints (union their interference), records the alias, colors the merged graph, then maps aliased values to the representative's home.

- [ ] **Step 1: Write the failing test**

```python
def test_coalesce_move_shares_register() -> None:
    """Non-interfering move-related values get the same register."""
    graph = {"a": set(), "b": set()}
    constraints = RegisterConstraints(pool=("ebx", "ecx"), allowed={}, precolored={})
    alloc = color(graph, {frozenset({"a", "b"})}, constraints, CostModel(spill_benefit={}, register_save_cost={}))
    assert alloc.homes["a"] == alloc.homes["b"]


def test_interfering_move_not_coalesced() -> None:
    """Move-related values that interfere must NOT share a register."""
    graph = {"a": {"b"}, "b": {"a"}}
    constraints = RegisterConstraints(pool=("ebx", "ecx"), allowed={}, precolored={})
    alloc = color(graph, {frozenset({"a", "b"})}, constraints, CostModel(spill_benefit={}, register_save_cost={}))
    assert alloc.homes["a"] != alloc.homes["b"]


def test_coalesce_not_done_when_unsafe() -> None:
    """Briggs: don't coalesce if it would create a high-degree uncolorable node."""
    # a-b move; a and b each interfere with c,d,e (degree would be 3 >= K=2 of
    # significant-degree neighbors).  With K=2, coalescing is unsafe -> skipped,
    # and the graph (a,b each deg 3) forces a spill rather than a bad merge.
    graph = {
        "a": {"c", "d", "e"}, "b": {"c", "d", "e"},
        "c": {"a", "b"}, "d": {"a", "b"}, "e": {"a", "b"},
    }
    constraints = RegisterConstraints(pool=("ebx", "ecx"), allowed={}, precolored={})
    alloc = color(graph, {frozenset({"a", "b"})}, constraints, CostModel(spill_benefit={}, register_save_cost={}))
    # The allocation is still valid: no two interfering values share a register.
    for node, neighbors in graph.items():
        if node in alloc.homes:
            for neighbor in neighbors:
                if neighbor in alloc.homes:
                    assert alloc.homes[node] != alloc.homes[neighbor]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "coalesce or interfering_move" -v`
Expected: FAIL (`test_coalesce_move_shares_register` — without coalescing, `a` and `b` may get different registers).

- [ ] **Step 3: Write minimal implementation**

Add a coalescing pre-pass and apply it at the top of `color` (before building `nodes`/`degree`). Insert this helper and rework `color`'s entry:

```python
def _conservative_coalesce(
    interference: dict[str, set[str]],
    moves: set[frozenset[str]],
    constraints: RegisterConstraints,
    /,
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Merge safe move-related pairs; return (merged graph, alias->representative).

    Briggs: merge ``{a, b}`` only when they do not interfere and the union of
    their neighbors has fewer than ``K`` members of significant degree
    (``>= K``).  Precolored endpoints and endpoints with incompatible ``allowed``
    sets are left un-coalesced (PR 1 keeps coalescing to the common
    same-constraint case; richer constrained coalescing is deferred).
    """
    pool_size = len(constraints.pool)
    graph: dict[str, set[str]] = {name: set(neighbors) for name, neighbors in interference.items()}
    alias: dict[str, str] = {}

    def find(name: str) -> str:
        while name in alias:
            name = alias[name]
        return name

    for pair in sorted(moves, key=lambda members: tuple(sorted(members))):
        first, second = sorted(pair)
        rep_a, rep_b = find(first), find(second)
        if rep_a == rep_b or rep_b in graph[rep_a]:
            continue
        if rep_a in constraints.precolored or rep_b in constraints.precolored:
            continue
        if constraints.allowed.get(rep_a) != constraints.allowed.get(rep_b):
            continue
        merged_neighbors = graph[rep_a] | graph[rep_b]
        significant = sum(1 for neighbor in merged_neighbors if len(graph[neighbor]) >= pool_size)
        if significant >= pool_size:
            continue
        # Merge rep_b into rep_a.
        for neighbor in graph[rep_b]:
            graph[neighbor].discard(rep_b)
            graph[neighbor].add(rep_a)
            graph[rep_a].add(neighbor)
        graph[rep_a].discard(rep_a)
        del graph[rep_b]
        alias[rep_b] = rep_a

    return graph, alias
```

Then change the start of `color` to coalesce first and the end to expand aliases:

```python
def color(interference, moves, constraints, costs, /):  # signature unchanged; body reworked
    """..."""  # keep the docstring
    merged_graph, alias = _conservative_coalesce(interference, moves, constraints)

    pool_size = len(constraints.pool)
    precolored = dict(constraints.precolored)
    nodes = [name for name in merged_graph if name not in precolored]
    degree = {name: set(merged_graph[name]) for name in nodes}

    # ... simplify loop (Task 5) and select loop (Task 6) UNCHANGED, but operate
    # on merged_graph and use _allowed_registers(name, constraints) ...
    # (replace every prior reference to `interference` inside color with
    #  `merged_graph`).

    # After select produces `homes` / `spilled` for representatives, expand
    # aliases so every coalesced value inherits its representative's outcome:
    def resolve(name: str) -> str:
        while name in alias:
            name = alias[name]
        return name

    final_homes: dict[str, str] = {}
    final_spilled: set[str] = set()
    for name in interference:
        representative = resolve(name)
        if representative in homes:
            final_homes[name] = homes[representative]
        else:
            final_spilled.add(name)
    return Allocation(homes=final_homes, spilled=frozenset(final_spilled))
```

**Implementer note:** carry the Task 4-6 simplify/select bodies into this reworked `color` verbatim, substituting `merged_graph` for `interference` in the degree/used-neighbor computations and the `_allowed_registers` lookups (the representative inherits an `allowed` set only when both endpoints shared it, which the coalesce guard enforced). Keep the precolored-neighbor consultation reading from `merged_graph`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "coalesce or interfering_move" -v`
Expected: PASS. Re-run the whole file:
Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add cc/regalloc.py tests/unit/test_cc_regalloc.py
git commit -m "feat(regalloc): conservative (Briggs) move coalescing

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Top-level `allocate` wiring + end-to-end test

**Files:**
- Modify: `cc/regalloc.py`
- Test: `tests/unit/test_cc_regalloc.py`

Wire `build_interference` → `color` in `allocate`, and add an end-to-end test over a synthetic function exercising the full path (interference, a move, a spill under pressure). Also assert the `RegallocError` propagates from `build_interference` for an unmodeled instruction (coverage discipline).

- [ ] **Step 1: Write the failing test**

```python
def test_allocate_end_to_end() -> None:
    """allocate() wires liveness->coloring: a,b interfere and get distinct registers."""
    body = [
        ir.Copy(destination="a", source=1),
        ir.Copy(destination="b", source=2),
        ir.BinaryOperation(destination="c", left="a", operation="+", right="b"),
        ir.Return(value="c"),
    ]
    allocatable = frozenset({"a", "b", "c"})
    constraints = RegisterConstraints(pool=("ebx", "ecx", "edx"), allowed={}, precolored={})
    costs = CostModel(spill_benefit={"a": 5, "b": 5, "c": 5}, register_save_cost={})
    alloc = allocate(_function(body), allocatable=allocatable, constraints=constraints, costs=costs)
    assert alloc.homes["a"] != alloc.homes["b"]
    assert alloc.spilled == frozenset()


def test_allocate_propagates_regalloc_error() -> None:
    """An unmodeled instruction surfaces as RegallocError, not a silent miss."""
    import pytest

    class _Bogus(ir.Jump):  # has VALUE_FIELDS=() but is not in _INERT/_MODELED
        pass

    # _Bogus subclasses Jump (inert), so build a body the use-extractor rejects:
    body = [object()]  # type: ignore[list-item]
    with pytest.raises((RegallocError, Exception)):
        allocate(_function(body), allocatable=frozenset(), constraints=RegisterConstraints(pool=(), allowed={}, precolored={}), costs=CostModel(spill_benefit={}, register_save_cost={}))
```

(The second test asserts the engine does not silently swallow an unknown shape; `build_cfg` may raise first — accept either, the point is "no silent success".)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "end_to_end or propagates_regalloc" -v`
Expected: FAIL (`allocate` raises `NotImplementedError`).

- [ ] **Step 3: Write minimal implementation**

Replace the `allocate` stub:

```python
def allocate(
    function: ir.Function,
    /,
    *,
    allocatable: frozenset[str],
    constraints: RegisterConstraints,
    costs: CostModel,
) -> Allocation:
    """Compute interference for *function* and color it — the end-to-end entry.

    PR 2/3 will call this with target-derived *constraints* / *costs* and then
    wire ``Allocation.homes`` into emission; PR 1 leaves it unconsumed.
    """
    interference = build_interference(function, allocatable=allocatable)
    return color(interference.graph, interference.moves, constraints, costs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -k "end_to_end or propagates_regalloc" -v`
Expected: PASS.

- [ ] **Step 5: Run the full module + unit suite, confirm byte-neutrality**

Run: `python3 -m pytest tests/unit/test_cc_regalloc.py -v` → Expected: PASS (all).
Run: `python3 -m pytest tests/unit/ -q` → Expected: PASS (no regression in the rest of the unit suite).
Confirm nothing imports the new module (byte-neutral): `rtk proxy grep -rn "import regalloc\|from cc.regalloc\|cc\.regalloc" cc/ | grep -v "cc/regalloc.py"` → Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add cc/regalloc.py tests/unit/test_cc_regalloc.py
git commit -m "feat(regalloc): top-level allocate() wiring + end-to-end tests

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] `python3 -m pytest tests/unit/test_cc_regalloc.py -v` — all engine tests pass.
- [ ] `python3 -m pytest tests/unit/ -q` — full unit suite green.
- [ ] No `cc/` file outside `cc/regalloc.py` imports the module (byte-neutral by construction; the byte-size gate is therefore not exercised by this PR, but run `python3 tests/test_cc_function_sizes.py` if available to confirm zero deltas).
- [ ] Dispatch the final code-reviewer over the whole `cc/regalloc.py` for algorithm correctness (especially the Briggs conservative-coalescing safety condition and the optimistic-spill select path) before finishing the branch.

## Notes for PR 2 (not part of PR 1)

PR 2 computes the real `RegisterConstraints` / `CostModel` from the target and wires `Allocation.homes` into emission for **locals/params** (replacing `_select_auto_pin_candidates`), gated at per-function bytes ≤ current baseline. The inputs map as: `pool` ← `compute_safe_pin_registers` ordering; `allowed` ← byte-alias (`low_byte is None` ⇒ exclude `si/di/bp`) + 16-bit index legality (`SI/DI` only); `precolored` ← regparm params (`EAX/EDX/ECX`) + accumulator handling; `register_save_cost` ← `register_clobber_counts` (per register) restricted to each value's `live_across_call`; `spill_benefit` ← reference counts. That wiring — and the parity gate — is the dominant-risk step and gets its own plan.
