"""Construction, string-bridge, and structural-equality checks for the cc Type hierarchy.

The structured ``Type`` classes are the representation for multidimensional
arrays and pointer-to-array (Plan 5 / multidim track B).  ``Type.from_string`` /
``Type.to_string`` bridge to the legacy flat type strings (``variable_types``,
struct-field ``type_name``) so existing string-keyed sites keep working while
consumers migrate incrementally.
"""

from __future__ import annotations

from cc.types import (
    ArrayType,
    PointerType,
    ScalarType,
    StructType,
    Type,
)


def test_array_recurses_via_pointee() -> None:
    """A 2-D array nests ArrayType inside ArrayType, innermost bracket innermost."""
    multidim = ArrayType(count=2, pointee=ArrayType(count=3, pointee=ScalarType(name="int")))
    assert multidim.count == 2
    assert multidim.pointee.count == 3
    assert multidim.pointee.pointee == ScalarType(name="int")


def test_from_string_double_pointer() -> None:
    """Stacked ``**`` nests PointerType over PointerType."""
    assert Type.from_string("int**") == PointerType(pointee=PointerType(pointee=ScalarType(name="int")))


def test_from_string_multidim_is_outer_first() -> None:
    """``int[2][3]`` parses the leftmost bracket as the outermost dimension."""
    assert Type.from_string("int[2][3]") == ArrayType(count=2, pointee=ArrayType(count=3, pointee=ScalarType(name="int")))


def test_from_string_pointer() -> None:
    """A trailing ``*`` becomes a PointerType over the pointee."""
    assert Type.from_string("char*") == PointerType(pointee=ScalarType(name="char"))


def test_from_string_scalar() -> None:
    """A bare type name becomes a ScalarType."""
    assert Type.from_string("unsigned short") == ScalarType(name="unsigned short")


def test_from_string_single_array() -> None:
    """The struct-field ``char[15]`` flat form becomes a sized ArrayType."""
    assert Type.from_string("char[15]") == ArrayType(count=15, pointee=ScalarType(name="char"))


def test_from_string_struct() -> None:
    """``struct foo`` becomes a StructType carrying the tag."""
    assert Type.from_string("struct foo") == StructType(tag="foo")


def test_from_string_struct_pointer() -> None:
    """``struct foo*`` is a PointerType over a StructType."""
    assert Type.from_string("struct foo*") == PointerType(pointee=StructType(tag="foo"))


def test_pointer_to_array_distinct_from_array_of_pointers() -> None:
    """``int(*)[3]`` (pointer to array) differs structurally from ``int*[3]``."""
    pointer_to_array = PointerType(pointee=ArrayType(count=3, pointee=ScalarType(name="int")))
    array_of_pointers = ArrayType(count=3, pointee=PointerType(pointee=ScalarType(name="int")))
    assert pointer_to_array != array_of_pointers


def test_scalar_holds_name() -> None:
    """ScalarType stores its base type name and is a Type subclass."""
    scalar = ScalarType(name="int")
    assert scalar.name == "int"


def test_string_bridge_round_trips_multidim() -> None:
    """from_string and to_string are inverses for a multidimensional array."""
    multidim = ArrayType(count=2, pointee=ArrayType(count=3, pointee=ScalarType(name="int")))
    assert Type.from_string(multidim.to_string()) == multidim


def test_structural_equality_and_hash() -> None:
    """Two identically-shaped Types compare equal and hash equal (value objects)."""
    left = ArrayType(count=4, pointee=ScalarType(name="char"))
    right = ArrayType(count=4, pointee=ScalarType(name="char"))
    assert left == right
    assert hash(left) == hash(right)


def test_to_string_round_trips_pointer() -> None:
    """A pointer serializes back to the ``pointee*`` flat form."""
    assert PointerType(pointee=ScalarType(name="char")).to_string() == "char*"


def test_to_string_round_trips_scalar() -> None:
    """A scalar serializes back to its bare name."""
    assert ScalarType(name="unsigned short").to_string() == "unsigned short"


def test_to_string_round_trips_single_array() -> None:
    """A single-dimension array serializes back to the ``T[N]`` flat form."""
    assert ArrayType(count=15, pointee=ScalarType(name="char")).to_string() == "char[15]"


def test_to_string_round_trips_struct_pointer() -> None:
    """A pointer-to-struct serializes back to the ``struct <tag>*`` flat form."""
    assert PointerType(pointee=StructType(tag="foo")).to_string() == "struct foo*"


def test_to_string_serializes_multidim() -> None:
    """A multidimensional array serializes outer-to-inner bracket order."""
    multidim = ArrayType(count=2, pointee=ArrayType(count=3, pointee=ScalarType(name="int")))
    assert multidim.to_string() == "int[2][3]"
