"""Structured type representation for the cc compiler.

cc historically carries types as flat strings (``variable_types[name]`` holds
the *element* type, struct fields bake the count into ``"char[15]"``, arrays
live in side-channel sets/dicts).  That cannot express true multidimensional
arrays (``int m[2][3]``) or pointer-to-array (``int (*p)[3]``), where the
distinction between *array of pointers* and *pointer to array* is structural.

This module introduces a small recursive class hierarchy as the representation
for those shapes:

* :class:`ScalarType`  — ``int``, ``unsigned short``, ``char`` …
* :class:`PointerType` — ``T*`` (recurse via ``pointee``)
* :class:`ArrayType`   — ``T[N]`` (recurse via ``pointee``; ``count=None`` is ``[]``)
* :class:`StructType`  — ``struct foo``

:meth:`Type.from_string` / :meth:`Type.to_string` bridge to the legacy flat
strings so existing string-keyed sites keep working while consumers migrate
incrementally.  The bridge covers exactly the shapes the old strings expressed
(scalars, pointers, struct tags, single ``[N]``); genuinely-new structured
shapes (multidimensional arrays, pointer-to-array) live as ``Type`` objects and
only need ``to_string`` to round-trip the cases the side-channels still key on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, kw_only=True, slots=True)
class Type:
    """Base class for every structured type.  Value object: frozen + hashable."""

    @classmethod
    def from_string(cls, text: str, /) -> Type:
        """Parse a legacy flat type string into a structured :class:`Type`.

        Handles the shapes the existing codebase produces: a bare scalar name,
        a ``struct <tag>``, trailing ``*`` pointer stacks, and a trailing
        ``[N]`` array suffix (the struct-field ``"char[15]"`` form, and the
        multidimensional ``"int[2][3]"`` form with the leftmost bracket
        outermost).
        """
        text = text.strip()
        if text.endswith("]"):
            # The trailing ``[..]`` run is the array dimensions, leftmost
            # outermost.  Split the element base off the first bracket, parse
            # the counts left-to-right, then nest outer-around-inner so
            # ``int[2][3]`` becomes ArrayType(2, ArrayType(3, int)).
            first_bracket = text.index("[")
            element = cls.from_string(text[:first_bracket])
            counts: list[int | None] = []
            rest = text[first_bracket:]
            while rest:
                close = rest.index("]")
                count_text = rest[1:close].strip()
                counts.append(int(count_text) if count_text else None)
                rest = rest[close + 1 :].strip()
            for count in reversed(counts):
                element = ArrayType(count=count, pointee=element)
            return element
        if text.endswith("*"):
            return PointerType(pointee=cls.from_string(text[:-1]))
        if text.startswith("struct "):
            return StructType(tag=text[len("struct ") :].strip())
        return ScalarType(name=text)

    def sizeof(self, *, pointer_width: int = 0, scalar_width: Callable[[str], int]) -> int:
        """Return the byte size of this type given target-width callables.

        Callers inject the target's ``scalar_width`` callable (maps a scalar or
        ``struct <tag>`` name to its byte count) and ``pointer_width`` integer
        so this module stays target-agnostic.
        """
        message = f"sizeof not implemented for {self!r}"
        raise NotImplementedError(message)

    def to_string(self) -> str:
        """Serialize this type back to its legacy flat string form."""
        message = f"cannot serialize type {self!r}"
        raise NotImplementedError(message)


@dataclass(frozen=True, kw_only=True, slots=True)
class ArrayType(Type):
    """An array ``pointee[count]``; ``count=None`` is the unsized ``[]`` form.

    A multidimensional array nests: ``int m[2][3]`` is
    ``ArrayType(2, ArrayType(3, ScalarType("int")))`` — the outermost
    bracket is the outermost ArrayType, the element type is the innermost
    ``pointee``.
    """

    count: int | None
    pointee: Type

    def sizeof(self, *, pointer_width: int = 0, scalar_width: Callable[[str], int]) -> int:
        """Return count * element size; raises ValueError for unsized arrays."""
        if self.count is None:
            message = "sizeof of an unsized array is ill-formed"
            raise ValueError(message)
        return self.count * self.pointee.sizeof(pointer_width=pointer_width, scalar_width=scalar_width)

    def to_string(self) -> str:
        """Serialize outer-to-inner so the element type prints once: ``int[2][3]``."""
        # Peel the array chain so the element type prints once and the
        # dimensions follow outer-to-inner: int[2][3], not int[3][2].
        brackets = ""
        element: Type = self
        while isinstance(element, ArrayType):
            brackets += f"[{element.count}]" if element.count is not None else "[]"
            element = element.pointee
        return f"{element.to_string()}{brackets}"


@dataclass(frozen=True, kw_only=True, slots=True)
class PointerType(Type):
    """A pointer ``pointee*``; ``pointee`` recurses into another Type."""

    pointee: Type

    def sizeof(self, *, pointer_width: int = 0, scalar_width: Callable[[str], int]) -> int:  # noqa: PLR6301
        """Return pointer_width regardless of pointee type."""
        return pointer_width

    def to_string(self) -> str:
        """Serialize to the ``pointee*`` flat form."""
        return f"{self.pointee.to_string()}*"


@dataclass(frozen=True, kw_only=True, slots=True)
class ScalarType(Type):
    """A scalar base type such as ``int``, ``char``, or ``unsigned short``."""

    name: str

    def sizeof(self, *, pointer_width: int = 0, scalar_width: Callable[[str], int]) -> int:
        """Return scalar_width(self.name)."""
        return scalar_width(self.name)

    def to_string(self) -> str:
        """Serialize to the bare scalar name."""
        return self.name


@dataclass(frozen=True, kw_only=True, slots=True)
class StructType(Type):
    """A ``struct <tag>`` aggregate."""

    tag: str

    def sizeof(self, *, pointer_width: int = 0, scalar_width: Callable[[str], int]) -> int:
        """Return scalar_width("struct <tag>")."""
        return scalar_width(f"struct {self.tag}")

    def to_string(self) -> str:
        """Serialize to the ``struct <tag>`` flat form."""
        return f"struct {self.tag}"
