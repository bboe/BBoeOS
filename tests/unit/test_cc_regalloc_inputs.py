"""The adapter maps cc economics into cc.regalloc engine inputs."""

from pathlib import Path

import pytest

from cc.cli import compile_source_homes
from cc.codegen.x86.generator import AutoPinEconomics
from cc.codegen.x86.regalloc_inputs import build_allocator_inputs


def test_allocatable_absent_from_interference_is_seeded_empty() -> None:
    """An allocatable value with no interference entry gets an empty set seeded."""
    economics = AutoPinEconomics(
        allocatable=frozenset({"x"}),
        reference_counts={"x": 2},
    )
    inputs = build_allocator_inputs(
        argument_affinity={},
        base_register=None,
        byte_pool=frozenset({"bx", "cx"}),
        economics=economics,
        interference={},
        pool=("bx", "cx"),
        precolored={},
        register_clobber_counts={"bx": 0, "cx": 0},
    )
    assert inputs.interference == {"x": set()}


@pytest.mark.xfail(reason="full byte/home parity is achieved in PR 2 Task 5", strict=False)
def test_allocator_homes_match_heuristic_on_libbboeos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocator-mode homes should converge to the heuristic's (parity target)."""
    source = min((Path(__file__).resolve().parents[2] / "user" / "libbboeos").glob("*.c"))
    monkeypatch.delenv("BBOE_REGALLOC", raising=False)
    heuristic = compile_source_homes(source=source)
    monkeypatch.setenv("BBOE_REGALLOC", "1")
    allocator = compile_source_homes(source=source)
    for function, homes in heuristic.items():
        assert allocator.get(function) == homes, function


def test_argument_affinity_discounts_convention_register_below_zero() -> None:
    """Argument affinity subtracts from the convention register's save cost (may go negative).

    With equal clobber costs on edx/ecx, an affinity of 2 for ecx makes its save
    cost ``base - 2`` (allowed below the edx cost and below zero), so the colorer
    prefers homing the value in ecx — the call site then skips the ``mov``.
    """
    economics = AutoPinEconomics(
        allocatable=frozenset({"v"}),
        reference_counts={"v": 5},
    )
    inputs = build_allocator_inputs(
        argument_affinity={"v": {"ecx": 2}},
        base_register=None,
        byte_pool=frozenset({"edx", "ecx"}),
        economics=economics,
        interference={"v": set()},
        pool=("edx", "ecx"),
        precolored={},
        register_clobber_counts={"ecx": 1, "edx": 1},
    )
    assert inputs.costs.register_save_cost["v"]["edx"] == 1
    assert inputs.costs.register_save_cost["v"]["ecx"] == 1 - 2
    assert inputs.costs.register_save_cost["v"]["ecx"] < inputs.costs.register_save_cost["v"]["edx"]


def test_base_register_penalty_applies_when_bp_is_not_pool_last() -> None:
    """Regression guard: bp index penalty applies even when bp is not pool[-1].

    With ``pool=("bx","cx","bp","di")`` and ``register_clobber_counts`` giving
    di clobber-count 2, the old ``pool[-1]`` inference would have picked di
    (clobber 2 != 0) and set ``base_register=None``, silently dropping the bp
    index penalty.  Passing ``base_register="bp"`` explicitly fixes this.
    """
    economics = AutoPinEconomics(
        allocatable=frozenset({"v"}),
        index_uses={"v": 3},
        reference_counts={"v": 5},
    )
    inputs = build_allocator_inputs(
        argument_affinity={},
        base_register="bp",
        byte_pool=frozenset({"bx", "cx"}),
        economics=economics,
        interference={"v": set()},
        pool=("bx", "cx", "bp", "di"),
        precolored={},
        register_clobber_counts={"bp": 0, "bx": 1, "cx": 1, "di": 2},
    )
    assert inputs.costs.register_save_cost["v"]["bp"] == 3
    assert inputs.costs.register_save_cost["v"]["di"] == 2


def test_byte_typed_value_is_restricted_to_byte_aliasable_registers() -> None:
    """A byte-typed value's allowed set is restricted to registers with an 8-bit alias."""
    economics = AutoPinEconomics(
        allocatable=frozenset({"ch"}),
        byte_typed=frozenset({"ch"}),
        reference_counts={"ch": 5},
    )
    inputs = build_allocator_inputs(
        argument_affinity={},
        base_register=None,
        byte_pool=frozenset({"bx", "cx"}),
        economics=economics,
        interference={"ch": set()},
        pool=("bx", "cx", "di", "bp"),
        precolored={},
        register_clobber_counts={"bp": 0, "bx": 0, "cx": 0, "di": 0},
    )
    assert inputs.constraints.allowed["ch"] == frozenset({"bx", "cx"})


def test_interference_is_restricted_to_allocatable() -> None:
    """Interference edges referencing non-allocatable values are dropped from the output graph."""
    economics = AutoPinEconomics(allocatable=frozenset({"a", "b"}), reference_counts={"a": 2, "b": 2})
    inputs = build_allocator_inputs(
        argument_affinity={},
        base_register=None,
        byte_pool=frozenset({"bx", "cx"}),
        economics=economics,
        interference={"a": {"b", "tmp"}, "b": {"a"}, "tmp": {"a"}},
        pool=("bx", "cx"),
        precolored={},
        register_clobber_counts={"bx": 0, "cx": 0},
    )
    assert inputs.interference == {"a": {"b"}, "b": {"a"}}


def test_non_byte_value_has_no_allowed_entry() -> None:
    """An allocatable word-width value must be absent from constraints.allowed.

    Absence means any pool register is legal (no restriction imposed).
    """
    economics = AutoPinEconomics(
        allocatable=frozenset({"word_var"}),
        reference_counts={"word_var": 4},
    )
    inputs = build_allocator_inputs(
        argument_affinity={},
        base_register=None,
        byte_pool=frozenset({"bx", "cx"}),
        economics=economics,
        interference={"word_var": set()},
        pool=("bx", "cx"),
        precolored={},
        register_clobber_counts={"bx": 0, "cx": 0},
    )
    assert "word_var" not in inputs.constraints.allowed


def test_precolored_is_passed_through() -> None:
    """Precolored assignments reach the RegisterConstraints unchanged."""
    economics = AutoPinEconomics(
        allocatable=frozenset({"param"}),
        reference_counts={"param": 1},
    )
    inputs = build_allocator_inputs(
        argument_affinity={},
        base_register=None,
        byte_pool=frozenset({"bx", "cx"}),
        economics=economics,
        interference={"param": set()},
        pool=("bx", "cx"),
        precolored={"param": "bx"},
        register_clobber_counts={"bx": 0, "cx": 0},
    )
    assert inputs.constraints.precolored == {"param": "bx"}


def test_save_cost_uses_clobber_minus_elision_and_bp_index_penalty() -> None:
    """Save cost is clobber-count minus elided pre-first-store clobbers; BP uses the index penalty."""
    economics = AutoPinEconomics(
        allocatable=frozenset({"hot"}),
        index_uses={"hot": 2},
        pre_store_clobbers={"hot": {"bx": 1}},
        reference_counts={"hot": 9},
    )
    inputs = build_allocator_inputs(
        argument_affinity={},
        base_register="bp",
        byte_pool=frozenset({"bx", "cx"}),
        economics=economics,
        interference={"hot": set()},
        pool=("bx", "cx", "di", "bp"),
        precolored={},
        register_clobber_counts={"bp": 0, "bx": 3, "cx": 4, "di": 0},
    )
    # bx: clobber 3 minus 1 elided = 2.  bp: zero clobber, index penalty 2.
    assert inputs.costs.register_save_cost["hot"]["bx"] == 2
    assert inputs.costs.register_save_cost["hot"]["bp"] == 2
    assert inputs.costs.register_save_cost["hot"]["di"] == 0
    assert inputs.costs.spill_benefit["hot"] == 9
    assert inputs.allocatable == frozenset({"hot"})
