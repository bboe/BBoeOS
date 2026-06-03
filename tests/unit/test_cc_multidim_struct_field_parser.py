"""Parser acceptance of nested subscripts on struct array-typed members.

Today the parser only consumes ONE ``[i]`` after a ``.member`` or
``->member`` access.  These tests pin the lifted behavior: every further
``[j]`` bracket is left-nested into a uniform :class:`SubscriptPlace`
chain over the :class:`MemberPlace`, with no intervening
:class:`DereferencePlace`.  A single bracket must produce the same
one-level :class:`SubscriptPlace` as before (byte-identical codegen).
"""

from __future__ import annotations

from cc import ast_nodes
from cc.lexer import tokenize
from cc.parser import Parser


def _first_body_statement(source: str, /) -> ast_nodes.Node:
    program = _parse(source)
    return program.functions[0].body[0]


def _parse(source: str, /) -> ast_nodes.Program:
    return Parser(tokenize(source)).parse_program()


def _return_place(source: str, /) -> ast_nodes.Place:
    """Return the ``.place`` of the PlaceLoad that a ``return expr;`` wraps."""
    program = _parse(source)
    body = program.functions[0].body
    return_statement = body[-1]
    assert isinstance(return_statement, ast_nodes.Return)
    load = return_statement.value
    assert isinstance(load, ast_nodes.PlaceLoad)
    return load.place


# ---------------------------------------------------------------------------
# Single subscript — byte-identical baseline
# ---------------------------------------------------------------------------


def test_dot_member_single_subscript_is_one_level() -> None:
    """``g.cells[1]`` still produces a one-level SubscriptPlace over MemberPlace."""
    source = "struct grid { int cells[6]; }; int main(void) { struct grid g; return g.cells[1]; }"
    place = _return_place(source)
    assert isinstance(place, ast_nodes.SubscriptPlace)
    assert isinstance(place.base, ast_nodes.MemberPlace)
    member = place.base
    assert isinstance(member.base, ast_nodes.VariablePlace)
    assert member.base.name == "g"
    assert member.member_name == "cells"
    assert place.index == ast_nodes.Int(value=1)
    # Must NOT be nested further.
    assert not isinstance(place.index, ast_nodes.SubscriptPlace)


# ---------------------------------------------------------------------------
# Two-subscript read via dot access
# ---------------------------------------------------------------------------


def test_dot_member_two_subscripts_nested_correctly() -> None:
    """``g.cells[1][2]`` parses to SubscriptPlace(SubscriptPlace(MemberPlace,...),2)."""
    source = "struct grid { int cells[6]; }; int main(void) { struct grid g; return g.cells[1][2]; }"
    place = _return_place(source)
    # Outer subscript
    assert isinstance(place, ast_nodes.SubscriptPlace)
    assert place.index == ast_nodes.Int(value=2)
    # Inner subscript
    inner = place.base
    assert isinstance(inner, ast_nodes.SubscriptPlace)
    assert inner.index == ast_nodes.Int(value=1)
    # Member base
    member = inner.base
    assert isinstance(member, ast_nodes.MemberPlace)
    assert member.member_name == "cells"
    assert isinstance(member.base, ast_nodes.VariablePlace)
    assert member.base.name == "g"


# ---------------------------------------------------------------------------
# Two-subscript read via arrow access
# ---------------------------------------------------------------------------


def test_arrow_member_two_subscripts_nested_correctly() -> None:
    """``p->cells[1][2]`` parses to SubscriptPlace(SubscriptPlace(MemberPlace(Deref,...),1),2)."""
    source = "struct grid { int cells[6]; }; int main(void) { struct grid* p; return p->cells[1][2]; }"
    place = _return_place(source)
    # Outer subscript
    assert isinstance(place, ast_nodes.SubscriptPlace)
    assert place.index == ast_nodes.Int(value=2)
    # Inner subscript
    inner = place.base
    assert isinstance(inner, ast_nodes.SubscriptPlace)
    assert inner.index == ast_nodes.Int(value=1)
    # Member base — arrow dereferences pointer directly (DereferencePlace(VariablePlace))
    member = inner.base
    assert isinstance(member, ast_nodes.MemberPlace)
    assert member.member_name == "cells"
    deref = member.base
    assert isinstance(deref, ast_nodes.DereferencePlace)
    assert isinstance(deref.pointer, ast_nodes.VariablePlace)
    assert deref.pointer.name == "p"


# ---------------------------------------------------------------------------
# Assignment store into dot-member two-subscript target
# ---------------------------------------------------------------------------


def test_dot_member_two_subscripts_assignment_produces_place_store() -> None:
    """``g.cells[1][2] = 7;`` parses without error and yields a PlaceStore."""
    source = "struct grid { int cells[6]; }; int main(void) { struct grid g; g.cells[1][2] = 7; return 0; }"
    # First body statement is the struct local declaration; second is the assignment.
    body = _parse(source).functions[0].body
    assign_statement = body[1]
    assert isinstance(assign_statement, ast_nodes.PlaceStore)
    outer = assign_statement.place
    assert isinstance(outer, ast_nodes.SubscriptPlace)
    assert outer.index == ast_nodes.Int(value=2)
    inner = outer.base
    assert isinstance(inner, ast_nodes.SubscriptPlace)
    assert inner.index == ast_nodes.Int(value=1)
    member = inner.base
    assert isinstance(member, ast_nodes.MemberPlace)
    assert member.member_name == "cells"
    assert isinstance(member.base, ast_nodes.VariablePlace)
    assert member.base.name == "g"
    assert assign_statement.value == ast_nodes.Int(value=7)
