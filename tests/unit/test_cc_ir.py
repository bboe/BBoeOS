"""Tests for invariants on the :mod:`cc.ir` instruction classes themselves."""

from __future__ import annotations

import dataclasses
import typing

from cc import ir


def test_every_instruction_subclass_declares_value_fields() -> None:
    """Every member of :data:`cc.ir.Instruction` declares ``VALUE_FIELDS``.

    Walkers like :func:`cc.ssa._iter_value_operands` and
    :func:`cc.ssa._map_value_operands` read ``VALUE_FIELDS`` to enumerate
    operand-bearing fields.  A new instruction added without declaring
    it would silently be skipped by every pass; this test fails-loud so
    the contract holds.
    """
    instruction_types = typing.get_args(ir.Instruction)
    missing = [cls for cls in instruction_types if not hasattr(cls, "VALUE_FIELDS")]
    assert missing == [], f"missing VALUE_FIELDS: {[cls.__name__ for cls in missing]}"


def test_value_fields_name_real_dataclass_fields() -> None:
    """Each ``VALUE_FIELDS`` entry names an actual field on its dataclass.

    Catches typos that would survive at import time but blow up later
    inside :func:`getattr` calls during optimization.
    """
    for cls in typing.get_args(ir.Instruction):
        declared = {field.name for field in dataclasses.fields(cls)}
        for field_name in cls.VALUE_FIELDS:
            assert field_name in declared, f"{cls.__name__}.VALUE_FIELDS names unknown field {field_name!r}"
