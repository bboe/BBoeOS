"""Unit tests for the pure AddressPlan dataclasses and helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cc.codegen.address_plan import AddressPlan, AddressTerm, scale_encodes_in_operand


def test_address_plan_defaults() -> None:
    """Default fields on a minimal ``AddressPlan`` have the expected zero values."""
    plan = AddressPlan(base="ebp-8", base_kind="frame")
    assert plan.bitfield is None
    assert plan.clobbers == frozenset()
    assert plan.decay_to_address is False
    assert plan.displacement == 0
    assert plan.terms == ()


def test_address_term_carries_value_and_scale() -> None:
    """``AddressTerm`` round-trips ``index_value`` and ``scale`` faithfully."""
    term = AddressTerm(index_value="_ir_3", scale=4)
    assert term.index_value == "_ir_3"
    assert term.scale == 4


def test_scale_encodes_in_operand_16_bit_only_unscaled() -> None:
    """16-bit addressing has no SIB byte: only scale-1 (unscaled) is valid."""
    assert scale_encodes_in_operand(bits=16, scale=1)
    assert not scale_encodes_in_operand(bits=16, scale=2)
    assert not scale_encodes_in_operand(bits=16, scale=4)


def test_scale_encodes_in_operand_32_bit() -> None:
    """Powers-of-two scales 1/2/4/8 encode in 32-bit SIB; others do not."""
    assert scale_encodes_in_operand(bits=32, scale=1)
    assert scale_encodes_in_operand(bits=32, scale=4)
    assert scale_encodes_in_operand(bits=32, scale=8)
    assert not scale_encodes_in_operand(bits=32, scale=3)
    assert not scale_encodes_in_operand(bits=32, scale=32)
