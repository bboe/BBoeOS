"""Unit tests for the pure AddressPlan dataclasses and helpers."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cc.cli import compile_module
from cc.codegen.address_plan import AddressPlan, AddressTerm, scale_encodes_in_operand
from cc.options import CompilerOptions

if TYPE_CHECKING:
    from cc.codegen.x86.generator import X86CodeGenerator

# The dot access lives in a helper function (not ``main``) because ``main``
# bypasses the IR lowering path — only IR-lowered bodies record Address ops.
DOT_MEMBER_SOURCE = """
struct point { int x; int y; };
struct point g;
int reader() {
    int value;
    value = g.y;
    return value;
}
int main() {
    return reader();
}
"""


def _generate(source_text: str, /, *, bits: int = 32) -> X86CodeGenerator:
    """Compile *source_text* and return the generator (plans inspectable)."""
    with tempfile.TemporaryDirectory(prefix="test_address_plan_") as work:
        source_path = Path(work) / "test.c"
        source_path.write_text(source_text)
        return compile_module(
            input_path=source_path,
            options=CompilerOptions(bits=bits),
            search_paths=(),
        )


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


def test_dot_member_load_produces_pure_displacement_plan() -> None:
    """``g.y`` lowers to one Address whose plan is a pure label+displacement."""
    generator = _generate(DOT_MEMBER_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "label"
    assert plan.displacement == 4  # offset of y
    assert plan.terms == ()
    assert plan.field_size == 4


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
