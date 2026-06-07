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


def _function(*, body: list[ir.Instruction]) -> ir.Function:
    """Wrap *body* in a minimal ir.Function for analysis."""
    ast = ast_nodes.Function(body=[], line=1, name="f", params=[])
    return ir.Function(ast_node=ast, body=body, strings=[])


def _no_costs() -> CostModel:
    return CostModel(register_save_cost={}, spill_benefit={})


def test_allocate_end_to_end() -> None:
    """allocate() wires liveness->coloring: a,b interfere and get distinct registers."""
    body = [
        ir.Copy(destination="a", source=1),
        ir.Copy(destination="b", source=2),
        ir.BinaryOperation(destination="c", left="a", operation="+", right="b"),
        ir.Return(value="c"),
    ]
    allocatable = frozenset({"a", "b", "c"})
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx", "edx"), precolored={})
    costs = CostModel(register_save_cost={}, spill_benefit={"a": 5, "b": 5, "c": 5})
    alloc = allocate(allocatable=allocatable, constraints=constraints, costs=costs, function=_function(body=body))
    assert alloc.homes["a"] != alloc.homes["b"]
    assert alloc.spilled == frozenset()


def test_allocate_propagates_regalloc_error() -> None:
    """An unmodeled instruction surfaces an error, not a silent miss."""
    import pytest  # noqa: PLC0415

    body = [object()]  # not a valid ir.Instruction
    with pytest.raises(Exception):  # noqa: B017, PT011
        allocate(
            allocatable=frozenset(),
            constraints=RegisterConstraints(allowed={}, pool=(), precolored={}),
            costs=CostModel(register_save_cost={}, spill_benefit={}),
            function=_function(body=body),  # type: ignore[arg-type]
        )


def test_benefit_gate_spills_when_save_cost_too_high() -> None:
    """A value whose benefit does not exceed its cheapest save cost is spilled."""
    graph = {"x": set()}
    constraints = RegisterConstraints(allowed={}, pool=("ebx",), precolored={})
    costs = CostModel(register_save_cost={"x": {"ebx": 2}}, spill_benefit={"x": 2})
    alloc = color(constraints=constraints, costs=costs, interference=graph, moves=set())
    assert "x" in alloc.spilled


def test_build_interference_reports_live_across_sets() -> None:
    """live_across[id(I)] is the set of allocatable names live through I.

    t0 is defined before the BinaryOperation and used after it, so it is
    live across; t1 is the BinaryOperation's own destination and must NOT
    be in its live-across set (it is written by I, not live through it).
    A last-use operand is dead through its consumer: the second
    BinaryOperation reads t1 for the final time, so its live-across set is
    empty — this pins the snapshot BEFORE the backward walk adds the
    instruction's own uses back into the live set.
    """
    binop = ir.BinaryOperation(destination="t1", left="t0", operation="+", right=2)
    last_use_binop = ir.BinaryOperation(destination="_discard1", left="t1", operation="+", right="t1")
    body = [
        ir.Copy(destination="t0", source=1),
        binop,
        ir.Copy(destination="_discard0", source="t0"),
        last_use_binop,
        ir.Return(value="_discard1"),
    ]
    allocatable = frozenset({"t0", "t1", "_discard0", "_discard1"})
    result = build_interference(allocatable=allocatable, function=_function(body=body))
    assert result.live_across[id(binop)] == frozenset({"t0"})
    assert result.live_across[id(last_use_binop)] == frozenset()


def test_coalesce_move_shares_register() -> None:
    """Coalescing forces move-related values onto the SAME register under pressure.

    p is precolored to ebx; a interferes with p so a must take ecx; b is free
    and (without coalescing) would take the first register ebx, differing from a.
    The {a,b} move must coalesce them onto ecx.
    """
    graph = {"a": {"p"}, "b": set(), "p": {"a"}}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={"p": "ebx"})
    alloc = color(constraints=constraints, costs=_no_costs(), interference=graph, moves={frozenset({"a", "b"})})
    assert alloc.homes["a"] == alloc.homes["b"] == "ecx"


def test_coalesce_not_done_when_unsafe() -> None:
    """Briggs blocks an unsafe merge that would otherwise force a spill.

    a-b is a move (non-interfering). Merging them forms an ab-c-d triangle that
    needs 3 colors and would spill at K=2.  Left un-merged the graph is a 2-color
    path, so a correct (Briggs-blocking) allocator spills nothing.  An allocator
    that always coalesced would spill here.
    """
    graph = {"a": {"c"}, "b": {"d"}, "c": {"a", "d"}, "d": {"b", "c"}}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={})
    alloc = color(constraints=constraints, costs=_no_costs(), interference=graph, moves={frozenset({"a", "b"})})
    assert alloc.spilled == frozenset()
    for node, neighbors in graph.items():
        for neighbor in neighbors:
            assert alloc.homes[node] != alloc.homes[neighbor]


def test_color_respects_allowed_set() -> None:
    """A value restricted to one register lands there."""
    graph = {"x": set(), "y": set()}
    constraints = RegisterConstraints(allowed={"x": frozenset({"ecx"})}, pool=("ebx", "ecx"), precolored={})
    alloc = color(constraints=constraints, costs=_no_costs(), interference=graph, moves=set())
    assert alloc.homes["x"] == "ecx"


def test_color_respects_precolored_neighbor() -> None:
    """A node adjacent to a precolored register avoids that register."""
    graph = {"p": {"q"}, "q": {"p"}}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={"p": "ebx"})
    alloc = color(constraints=constraints, costs=_no_costs(), interference=graph, moves=set())
    assert alloc.homes["p"] == "ebx"
    assert alloc.homes["q"] == "ecx"


def test_color_triangle_three_registers() -> None:
    """A 3-clique colors with 3 registers, all distinct."""
    graph = {"a": {"b", "c"}, "b": {"a", "c"}, "c": {"a", "b"}}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx", "edx"), precolored={})
    alloc = color(constraints=constraints, costs=_no_costs(), interference=graph, moves=set())
    assert alloc.spilled == frozenset()
    homes = alloc.homes
    assert homes["a"] != homes["b"] != homes["c"] != homes["a"]
    assert set(homes.values()) <= {"ebx", "ecx", "edx"}


def test_contradictory_precolor_raises() -> None:
    """Two interfering values precolored to the same register is rejected loudly."""
    import pytest  # noqa: PLC0415

    graph = {"c": {"f"}, "f": {"c"}}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={"c": "ebx", "f": "ebx"})
    with pytest.raises(RegallocError):
        color(constraints=constraints, costs=_no_costs(), interference=graph, moves=set())


def test_defs_and_uses_cover_value_fields_and_base_names() -> None:
    """Defs = destination; uses = value-field reads + Index/IndexAssign base names."""
    from cc.regalloc import _instruction_defs, _instruction_uses  # noqa: PLC0415, PLC2701

    binop = ir.BinaryOperation(destination="_ir_0", left="a", operation="+", right=2)
    assert _instruction_defs(instruction=binop) == ("_ir_0",)
    assert set(_instruction_uses(instruction=binop)) == {"a"}  # the literal 2 is not a name

    index = ir.Index(base="arr", destination="_ir_1", index="i")
    assert _instruction_defs(instruction=index) == ("_ir_1",)
    assert set(_instruction_uses(instruction=index)) == {"arr", "i"}  # base name included

    store = ir.IndexAssign(base="arr", index="j", source="v")
    assert _instruction_defs(instruction=store) == ()  # IndexAssign defines nothing
    assert set(_instruction_uses(instruction=store)) == {"arr", "j", "v"}

    call = ir.Call(args=("x", 3), destination="_ir_2", name="f")
    assert _instruction_defs(instruction=call) == ("_ir_2",)
    assert set(_instruction_uses(instruction=call)) == {"x"}


def test_interference_move_pair_recorded() -> None:
    """A Copy between two allocatable values is a coalesce candidate."""
    body = [
        ir.Copy(destination="a", source=1),
        ir.Copy(destination="b", source="a"),
        ir.Return(value="b"),
    ]
    result = build_interference(allocatable=frozenset({"a", "b"}), function=_function(body=body))
    assert frozenset({"a", "b"}) in result.moves


def test_interference_two_simultaneously_live_values() -> None:
    """Two values both live at a point interfere; a dead-then-reused value does not."""
    body = [
        ir.Copy(destination="a", source=1),
        ir.Copy(destination="b", source=2),
        ir.BinaryOperation(destination="c", left="a", operation="+", right="b"),
        ir.Return(value="c"),
    ]
    allocatable = frozenset({"a", "b", "c"})
    result = build_interference(allocatable=allocatable, function=_function(body=body))
    assert "b" in result.graph["a"] and "a" in result.graph["b"]
    assert result.graph.get("c", set()) == set()


def test_interfering_move_not_coalesced() -> None:
    """Move-related values that interfere must NOT share a register."""
    graph = {"a": {"b"}, "b": {"a"}}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={})
    alloc = color(constraints=constraints, costs=_no_costs(), interference=graph, moves={frozenset({"a", "b"})})
    assert alloc.homes["a"] != alloc.homes["b"]


def test_live_across_call_counted() -> None:
    """A value live across a Call is counted for save-cost."""
    body = [
        ir.Copy(destination="keep", source=1),
        ir.Call(args=(), destination="_ir_0", name="f"),
        ir.Return(value="keep"),
    ]
    result = build_interference(allocatable=frozenset({"keep", "_ir_0"}), function=_function(body=body))
    assert result.live_across_call.get("keep", 0) == 1


def test_no_spill_when_pressure_fits() -> None:
    """A 2-clique with 2 registers spills nothing even at equal benefit."""
    graph = {"a": {"b"}, "b": {"a"}}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={})
    alloc = color(constraints=constraints, costs=_no_costs(), interference=graph, moves=set())
    assert alloc.spilled == frozenset()


def test_non_allocatable_names_absent_from_graph() -> None:
    """Globals / labels are not allocatable and never become graph nodes."""
    body = [
        ir.Index(base="g_global", destination="_ir_0", index="i"),
        ir.Return(value="_ir_0"),
    ]
    result = build_interference(allocatable=frozenset({"_ir_0", "i"}), function=_function(body=body))
    assert "g_global" not in result.graph


def test_public_types_construct() -> None:
    """The public dataclasses construct with their documented fields."""
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={})
    costs = CostModel(register_save_cost={}, spill_benefit={})
    allocation = Allocation(homes={"x": "ebx"}, spilled=frozenset({"y"}))
    assert constraints.pool == ("ebx", "ecx")
    assert costs.spill_benefit == {}
    assert allocation.homes["x"] == "ebx"
    assert "y" in allocation.spilled
    inter = InterferenceResult(graph={"x": set()}, live_across={}, live_across_call={"x": 1}, moves=set())
    assert inter.graph == {"x": set()}
    assert inter.live_across == {}
    assert inter.live_across_call["x"] == 1


def test_select_prefers_cheapest_save_cost() -> None:
    """Among free registers, pick the one with the lowest save cost."""
    graph = {"x": set()}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={})
    costs = CostModel(register_save_cost={"x": {"ebx": 5, "ecx": 0}}, spill_benefit={"x": 100})
    alloc = color(constraints=constraints, costs=costs, interference=graph, moves=set())
    assert alloc.homes["x"] == "ecx"


def test_self_copy_does_not_crash() -> None:
    """A self-copy Copy(x, x) is not a coalesce candidate and must not crash."""
    body = [ir.Copy(destination="a", source="a"), ir.Return(value="a")]
    result = build_interference(allocatable=frozenset({"a"}), function=_function(body=body))
    assert all(len(pair) == 2 for pair in result.moves)  # no degenerate size-1 move
    alloc = allocate(
        allocatable=frozenset({"a"}),
        constraints=RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={}),
        costs=CostModel(register_save_cost={}, spill_benefit={}),
        function=_function(body=body),
    )
    assert "a" in alloc.homes  # no crash; a still gets a register


def test_spill_lowest_benefit_under_pressure() -> None:
    """A 3-clique with only 2 registers spills the lowest-benefit node."""
    graph = {"a": {"b", "c"}, "b": {"a", "c"}, "c": {"a", "b"}}
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={})
    costs = CostModel(register_save_cost={}, spill_benefit={"a": 10, "b": 10, "c": 1})
    alloc = color(constraints=constraints, costs=costs, interference=graph, moves=set())
    assert alloc.spilled == frozenset({"c"})
    assert alloc.homes["a"] != alloc.homes["b"]


def test_unmodeled_instruction_raises() -> None:
    """An IR shape with no def/use rule raises RegallocError (fail loud)."""
    import pytest  # noqa: PLC0415

    from cc.regalloc import _instruction_uses  # noqa: PLC0415, PLC2701

    class _Bogus:
        VALUE_FIELDS = ()

    with pytest.raises(RegallocError):
        _instruction_uses(instruction=_Bogus())


def test_uses_walk_opaque_block_ast() -> None:
    """Block/Access reads are discovered by walking the wrapped AST for Var names."""
    from cc.regalloc import _instruction_uses  # noqa: PLC0415, PLC2701

    node = ast_nodes.Assign(expr=ast_nodes.Var(line=1, name="k"), line=1, name="_ir_3")
    block = ir.Block(node=node)
    assert "k" in set(_instruction_uses(instruction=block))
