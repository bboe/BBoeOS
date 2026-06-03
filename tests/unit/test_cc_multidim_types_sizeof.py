"""Recursive sizeof on the cc Type hierarchy (target widths injected by caller)."""

from __future__ import annotations

from cc.types import ArrayType, PointerType, ScalarType, StructType


def _width(name: str, /) -> int:
    """Test stand-in for the target's scalar/struct/pointer byte widths."""
    return {"char": 1, "unsigned short": 2, "int": 4, "struct point": 8}[name]


def test_array_sizeof_is_count_times_element() -> None:
    """A 10-element int array has sizeof == 40."""
    array = ArrayType(count=10, pointee=ScalarType(name="int"))
    assert array.sizeof(pointer_width=4, scalar_width=_width) == 40


def test_multidim_sizeof_is_product_of_dimensions() -> None:
    """A 2x3 int array has sizeof == 24 (all dimensions multiplied)."""
    array = ArrayType(count=2, pointee=ArrayType(count=3, pointee=ScalarType(name="int")))
    assert array.sizeof(pointer_width=4, scalar_width=_width) == 24


def test_pointer_sizeof_is_pointer_width() -> None:
    """A pointer's sizeof equals the injected pointer_width regardless of pointee."""
    assert PointerType(pointee=ScalarType(name="char")).sizeof(pointer_width=4, scalar_width=_width) == 4


def test_scalar_sizeof_uses_callable() -> None:
    """A scalar's sizeof delegates to the scalar_width callable."""
    assert ScalarType(name="unsigned short").sizeof(scalar_width=_width) == 2


def test_struct_sizeof_uses_callable() -> None:
    """A struct's sizeof calls scalar_width with the ``struct <tag>`` key."""
    assert StructType(tag="point").sizeof(pointer_width=4, scalar_width=_width) == 8
