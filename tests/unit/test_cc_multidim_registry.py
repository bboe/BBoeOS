"""Array-type registry records structured ArrayType for array declarations."""

from __future__ import annotations

from cc.ast_nodes import Int
from cc.codegen.x86.generator import X86CodeGenerator
from cc.types import ArrayType, ScalarType


def _generator() -> X86CodeGenerator:
    """Construct a default X86CodeGenerator for unit testing.

    Uses the no-arg form (all defaults: bits=16, user mode, flat binary)
    which is the same shape as the rest of the unit test suite.
    """
    return X86CodeGenerator()


def test_register_multidim_builds_nested_array_type() -> None:
    """_register_array_type with two dimensions builds a nested ArrayType chain."""
    generator = _generator()
    generator._register_array_type(  # ruff:ignore[private-member-access]
        "matrix",
        dimensions=[Int(value=2), Int(value=3)],
        line=1,
        type_name="int",
    )
    assert generator.array_types["matrix"] == ArrayType(
        count=2,
        pointee=ArrayType(count=3, pointee=ScalarType(name="int")),
    )


def test_register_stores_entry_under_given_name() -> None:
    """_register_array_type stores the result keyed by the variable name."""
    generator = _generator()
    generator._register_array_type(  # ruff:ignore[private-member-access]
        "table",
        dimensions=[Int(value=8), Int(value=16)],
        line=1,
        type_name="unsigned char",
    )
    assert "table" in generator.array_types
    assert generator.array_types["table"] == ArrayType(
        count=8,
        pointee=ArrayType(count=16, pointee=ScalarType(name="unsigned char")),
    )


def test_register_three_dimensions_builds_triple_nested_array_type() -> None:
    """_register_array_type with three dimensions builds a triple-nested ArrayType."""
    generator = _generator()
    generator._register_array_type(  # ruff:ignore[private-member-access]
        "cube",
        dimensions=[Int(value=4), Int(value=5), Int(value=6)],
        line=1,
        type_name="char",
    )
    assert generator.array_types["cube"] == ArrayType(
        count=4,
        pointee=ArrayType(
            count=5,
            pointee=ArrayType(count=6, pointee=ScalarType(name="char")),
        ),
    )
