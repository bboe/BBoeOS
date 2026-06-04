"""Tests for cc.regalloc — IR-level liveness/interference + Chaitin-Briggs coloring.

Each test builds a small flat IR by hand (avoiding the AST + Builder roundtrip)
and synthetic constraint/cost inputs, so the expected allocation is obvious from
the test source.  The engine is pure: no codegen, no target import.
"""

from __future__ import annotations

from cc import ast_nodes, ir
from cc.regalloc import Allocation, CostModel, InterferenceResult, RegallocError, RegisterConstraints


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


def test_public_types_construct() -> None:
    """The public dataclasses construct with their documented fields."""
    constraints = RegisterConstraints(allowed={}, pool=("ebx", "ecx"), precolored={})
    costs = CostModel(register_save_cost={}, spill_benefit={})
    allocation = Allocation(homes={"x": "ebx"}, spilled=frozenset({"y"}))
    assert constraints.pool == ("ebx", "ecx")
    assert costs.spill_benefit == {}
    assert allocation.homes["x"] == "ebx"
    assert "y" in allocation.spilled
    inter = InterferenceResult(graph={"x": set()}, live_across_call={"x": 1}, moves=set())
    assert inter.graph == {"x": set()}
    assert inter.live_across_call["x"] == 1


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

    node = ast_nodes.Assign(line=1, name="_ir_3", expr=ast_nodes.Var(line=1, name="k"))
    block = ir.Block(node=node)
    assert "k" in set(_instruction_uses(instruction=block))
