"""Tests for cc.regalloc — IR-level liveness/interference + Chaitin-Briggs coloring.

Each test builds a small flat IR by hand (avoiding the AST + Builder roundtrip)
and synthetic constraint/cost inputs, so the expected allocation is obvious from
the test source.  The engine is pure: no codegen, no target import.
"""

from __future__ import annotations

from cc.regalloc import Allocation, CostModel, InterferenceResult, RegisterConstraints


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
