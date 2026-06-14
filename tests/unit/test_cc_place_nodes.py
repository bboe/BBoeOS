"""Construction + structural-equality checks for the Place node family."""

from __future__ import annotations

from cc import ast_nodes


def test_chained_member_place_nests() -> None:
    """a->b.c is MemberPlace(MemberPlace(DereferencePlace(VariablePlace), b), c)."""
    place = ast_nodes.MemberPlace(
        base=ast_nodes.MemberPlace(
            base=ast_nodes.DereferencePlace(pointer=ast_nodes.VariablePlace(name="a")),
            member_name="b",
        ),
        member_name="c",
    )
    assert place.member_name == "c"
    assert place.base.member_name == "b"


def test_dereference_place_takes_any_expression() -> None:
    """DereferencePlace.pointer accepts any Node."""
    place = ast_nodes.DereferencePlace(pointer=ast_nodes.Var(name="p"))
    assert isinstance(place.pointer, ast_nodes.Node)
    assert place.pointer.name == "p"


def test_member_place_over_dereference_models_arrow() -> None:
    """ptr->field is MemberPlace(DereferencePlace(VariablePlace), field)."""
    place = ast_nodes.MemberPlace(
        base=ast_nodes.DereferencePlace(pointer=ast_nodes.VariablePlace(name="ptr")),
        member_name="field",
    )
    assert isinstance(place.base, ast_nodes.DereferencePlace)
    assert place.base.pointer.name == "ptr"


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


def test_place_address_of_over_variable() -> None:
    """&x is PlaceAddressOf(VariablePlace) once AddressOf folds into Place."""
    node = ast_nodes.PlaceAddressOf(place=ast_nodes.VariablePlace(name="x"))
    assert isinstance(node.place, ast_nodes.VariablePlace)
    assert node.place.name == "x"


def test_place_call_over_subscript_and_dereference() -> None:
    """arr[i](args) and (*fp)(args) are PlaceCall over a SubscriptPlace / DereferencePlace."""
    indexed = ast_nodes.PlaceCall(
        args=[ast_nodes.Int(value=3)],
        place=ast_nodes.SubscriptPlace(base=ast_nodes.VariablePlace(name="fns"), index=ast_nodes.Var(name="i")),
    )
    assert isinstance(indexed.place, ast_nodes.SubscriptPlace)
    assert indexed.args == [ast_nodes.Int(value=3)]
    through_pointer = ast_nodes.PlaceCall(args=[], place=ast_nodes.DereferencePlace(pointer=ast_nodes.Var(name="fp")))
    assert isinstance(through_pointer.place, ast_nodes.DereferencePlace)


def test_place_increment_decrement_over_subscript() -> None:
    """a[i]++ is PlaceIncrementDecrement(SubscriptPlace(VariablePlace, index))."""
    node = ast_nodes.PlaceIncrementDecrement(
        delta=-1,
        is_postfix=False,
        place=ast_nodes.SubscriptPlace(base=ast_nodes.VariablePlace(name="a"), index=ast_nodes.Var(name="i")),
    )
    assert isinstance(node.place, ast_nodes.SubscriptPlace)
    assert isinstance(node.place.base, ast_nodes.VariablePlace)


def test_place_increment_decrement_over_variable() -> None:
    """x++ is PlaceIncrementDecrement(VariablePlace) once IncrementDecrement folds into Place."""
    node = ast_nodes.PlaceIncrementDecrement(delta=1, is_postfix=True, place=ast_nodes.VariablePlace(name="x"))
    assert node.delta == 1
    assert node.is_postfix is True
    assert isinstance(node.place, ast_nodes.VariablePlace)


def test_place_load_is_integer_operand() -> None:
    """PlaceLoad is classified as an IntegerOperand."""
    load = ast_nodes.PlaceLoad(place=ast_nodes.VariablePlace(name="x"))
    assert isinstance(load, ast_nodes.IntegerOperand)


def test_place_load_over_standalone_dereference() -> None:
    """*p (read) is PlaceLoad(DereferencePlace(Var)) — a standalone deref Place."""
    load = ast_nodes.PlaceLoad(place=ast_nodes.DereferencePlace(pointer=ast_nodes.Var(name="p")))
    assert isinstance(load.place, ast_nodes.DereferencePlace)
    assert load.place.pointer.name == "p"


def test_place_store_carries_value() -> None:
    """PlaceStore retains the stored value node."""
    store = ast_nodes.PlaceStore(
        place=ast_nodes.VariablePlace(name="x"),
        value=ast_nodes.Int(value=5),
    )
    assert store.value == ast_nodes.Int(value=5)


def test_place_store_over_standalone_dereference() -> None:
    """*p = v is PlaceStore(DereferencePlace(Var), value)."""
    store = ast_nodes.PlaceStore(
        place=ast_nodes.DereferencePlace(pointer=ast_nodes.Var(name="p")),
        value=ast_nodes.Int(value=5),
    )
    assert isinstance(store.place, ast_nodes.DereferencePlace)
    assert store.value == ast_nodes.Int(value=5)


def test_subscript_over_dereference_models_double_index() -> None:
    """a[i][j] is SubscriptPlace(DereferencePlace(Index(a, i)), j)."""
    place = ast_nodes.SubscriptPlace(
        base=ast_nodes.DereferencePlace(
            pointer=ast_nodes.Index(array=ast_nodes.Var(name="a"), index=ast_nodes.Var(name="i")),
        ),
        index=ast_nodes.Var(name="j"),
    )
    assert isinstance(place.base, ast_nodes.DereferencePlace)
    assert isinstance(place.base.pointer, ast_nodes.Index)


def test_subscript_over_member_models_pointer_field_index() -> None:
    """ptr->field[i] is SubscriptPlace(MemberPlace(...), index)."""
    place = ast_nodes.SubscriptPlace(
        base=ast_nodes.MemberPlace(
            base=ast_nodes.DereferencePlace(pointer=ast_nodes.VariablePlace(name="ptr")),
            member_name="field",
        ),
        index=ast_nodes.Var(name="i"),
    )
    assert isinstance(place.base, ast_nodes.MemberPlace)
    assert isinstance(place.base.base, ast_nodes.DereferencePlace)


def test_subscript_place_recurses() -> None:
    """SubscriptPlace.base is a Place and preserves object identity."""
    inner = ast_nodes.VariablePlace(name="arr")
    place = ast_nodes.SubscriptPlace(base=inner, index=ast_nodes.Int(value=2))
    assert place.base is inner
    assert isinstance(place.base, ast_nodes.Place)


def test_variable_place_holds_name() -> None:
    """VariablePlace stores its name and is a Place subclass."""
    place = ast_nodes.VariablePlace(name="arr")
    assert place.name == "arr"
    assert isinstance(place, ast_nodes.Place)
