"""The extracted economics bundle reproduces the inputs auto-pin consumes."""

from cc.ast_nodes import Assign, Int, PlaceAddressOf, VarDecl, VariablePlace
from cc.codegen.x86.generator import X86CodeGenerator
from cc.options import CompilerOptions


def _generator() -> X86CodeGenerator:
    return X86CodeGenerator(CompilerOptions(bits=32, target_mode="user"))


def test_address_taken_local_is_excluded_from_allocatable() -> None:
    """A local whose address is taken must not appear in the allocatable set."""
    body = [
        VarDecl(init=Int(value=0), line=1, name="slot", type_name="int"),
        VarDecl(
            init=PlaceAddressOf(place=VariablePlace(name="slot")),
            line=2,
            name="ptr",
            type_name="int",
        ),
    ]
    generator = _generator()
    generator.safe_pin_registers = generator.compute_safe_pin_registers(body, parameters=[])
    economics = generator._compute_pin_economics(body=body, parameters=[])  # noqa: SLF001
    assert "slot" not in economics.allocatable
    assert "slot" in economics.address_taken


def test_reference_counts_and_allocatable_for_simple_body() -> None:
    """Economics bundle marks a simple local as allocatable with a non-zero reference count."""
    # int total = 0; total = 1;  -> 'total' referenced once (the Assign node), eligible, no index use.
    body = [
        VarDecl(init=Int(value=0), line=1, name="total", type_name="int"),
        Assign(expr=Int(value=1), line=2, name="total"),
    ]
    generator = _generator()
    generator.safe_pin_registers = generator.compute_safe_pin_registers(body, parameters=[])
    economics = generator._compute_pin_economics(body=body, parameters=[])  # noqa: SLF001
    assert "total" in economics.allocatable
    assert economics.reference_counts.get("total", 0) == 1
    assert economics.index_uses.get("total", 0) == 0
    assert "total" not in economics.address_taken
