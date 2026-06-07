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

# Member accesses live in helper functions (not ``main``) because ``main``
# bypasses the IR lowering path — only IR-lowered bodies record Address ops.
ARROW_MEMBER_SOURCE = """
struct node { int value; struct node *next; };
int read_value(struct node *n) {
    int out;
    out = n->value;
    return out;
}
"""

ARROW_STORE_SOURCE = """
struct node { int value; };
void write_value(struct node *n, int v) {
    n->value = v;
}
"""

CHAINED_DOT_SOURCE = """
struct inner { int a; int b; };
struct outer { int pad; struct inner mid; };
struct outer g;
int reader() {
    int value;
    value = g.mid.b;
    return value;
}
int main() {
    return reader();
}
"""

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

MULTI_LEVEL_ARROW_SOURCE = """
struct inner { int a; int b; };
struct outer { int pad; struct inner mid; };
int reader(struct outer *p) {
    int value;
    value = p->mid.b;
    return value;
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


def test_arrow_member_load_plans_pointer_base() -> None:
    """``n->value`` plans a "pointer" base whose materialization is the SI-or-BX load."""
    generator = _generate(ARROW_MEMBER_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "pointer"
    assert plan.base == "n"
    assert plan.base_preserves_accumulator is True
    assert plan.base_is_static is False


def test_arrow_member_store_consumes_native_plan() -> None:
    """``n->value = v`` plans an accumulator-preserving pointer base and stores natively.

    The plan facts pin the store ordering (rhs first, then the bare
    ``mov ebx, [n]`` base load); the asm-shape assertions pin the native
    materialization to exactly one base load and one register-base store,
    with the rhs evaluated BEFORE the base load (the accumulator-preserving
    ordering).  Byte-for-byte parity with the legacy path is enforced by
    ``tests/test_cc_function_sizes.py``.
    """
    generator = _generate(ARROW_STORE_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "pointer"
    assert plan.base_is_static is False
    assert plan.base_preserves_accumulator is True
    body = generator.output.split("write_value:")[1]
    lines = [line.strip() for line in body.splitlines()]
    base_loads = [line for line in lines if line.startswith("mov ebx, [")]
    assert base_loads == ["mov ebx, [ebp-4]"]
    assert lines.count("mov [ebx], eax") == 1
    assert lines.index("mov eax, [ebp-8]") < lines.index("mov ebx, [ebp-4]")
    # No spill: the accumulator-preserving ordering never saves the rhs
    # around the base load (a push/pop pair here would be a +2-byte
    # regression the byte gate would also catch corpus-wide).
    assert "push eax" not in lines


def test_chained_dot_member_load_records_no_address_op() -> None:
    """``g.mid.b`` never lowers to ``ir.Address`` — it stays on legacy ``ir.Access``.

    The IR lowering predicates cover dot, arrow, and arrow-then-dot member
    loads only; a ``MemberPlace(MemberPlace(VariablePlace))`` chained-dot read
    is not gated in, so there is no Address op (and hence no plan) to
    materialize.  The legacy chained arm in ``_resolve_member_place_info``
    handles it whole.
    """
    generator = _generate(CHAINED_DOT_SOURCE)
    assert generator._ir_address_ops == {}  # noqa: SLF001
    assert generator._ir_address_plans == {}  # noqa: SLF001


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


def test_multi_level_arrow_load_plans_nested_plan_base() -> None:
    """``p->mid.b`` plans a "plan" base nesting the arrow plan for ``p->mid``.

    Mirrors the legacy chained arm of ``_resolve_member_place_info``: the
    inner ``p->mid`` struct-value member address is loaded (decayed ``lea``)
    and moved into BX, then the outer field offset rides the register base.
    """
    generator = _generate(MULTI_LEVEL_ARROW_SOURCE)
    plans = list(generator._ir_address_plans.values())  # noqa: SLF001
    assert len(plans) == 1
    plan = plans[0]
    assert plan.base_kind == "plan"
    assert plan.base_is_static is False
    assert plan.base_preserves_accumulator is False
    assert plan.displacement == 4  # offset of b within struct inner
    inner = plan.base
    assert isinstance(inner, AddressPlan)
    assert inner.base_kind == "pointer"
    assert inner.base == "p"
    assert inner.displacement == 4  # offset of mid within struct outer
    assert inner.decay_to_address is True  # struct-value member decays via lea


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
