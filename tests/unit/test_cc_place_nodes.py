"""Construction + structural-equality checks for the Place node family."""

from __future__ import annotations

from cc import ast_nodes


def test_variable_place_holds_name() -> None:
    """VariablePlace stores its name and is a Place subclass."""
    place = ast_nodes.VariablePlace(name="arr")
    assert place.name == "arr"
    assert isinstance(place, ast_nodes.Place)


def test_subscript_place_recurses() -> None:
    """SubscriptPlace.base is a Place and preserves object identity."""
    inner = ast_nodes.VariablePlace(name="arr")
    place = ast_nodes.SubscriptPlace(base=inner, index=ast_nodes.Int(value=2))
    assert place.base is inner
    assert isinstance(place.base, ast_nodes.Place)


def test_member_place_recurses_over_subscript() -> None:
    """MemberPlace can wrap a SubscriptPlace as its base."""
    place = ast_nodes.MemberPlace(
        base=ast_nodes.SubscriptPlace(
            base=ast_nodes.VariablePlace(name="arr"),
            index=ast_nodes.Var(name="i"),
        ),
        member_name="field",
    )
    assert place.member_name == "field"
    assert isinstance(place.base, ast_nodes.SubscriptPlace)


def test_dereference_place_takes_any_expression() -> None:
    """DereferencePlace.pointer accepts any Node."""
    place = ast_nodes.DereferencePlace(pointer=ast_nodes.Var(name="p"))
    assert isinstance(place.pointer, ast_nodes.Node)
    assert place.pointer.name == "p"


def test_place_load_is_integer_operand() -> None:
    """PlaceLoad is classified as an IntegerOperand."""
    load = ast_nodes.PlaceLoad(place=ast_nodes.VariablePlace(name="x"))
    assert isinstance(load, ast_nodes.IntegerOperand)


def test_place_store_carries_value() -> None:
    """PlaceStore retains the stored value node."""
    store = ast_nodes.PlaceStore(
        place=ast_nodes.VariablePlace(name="x"),
        value=ast_nodes.Int(value=5),
    )
    assert store.value == ast_nodes.Int(value=5)
