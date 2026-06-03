"""Parser shape for multidimensional subscript access ``m[i][j]``.

Tasks 5/6 unify every 2+-subscript access into a left-nested
:class:`SubscriptPlace` chain over a :class:`VariablePlace` base with NO
intervening :class:`DereferencePlace`.  Codegen then dispatches on the
declared type: a registered multidim array gets row-major addressing; an
array-of-pointers reconstructs the legacy deref shape for byte-identical
output.  A single subscript ``m[i]`` keeps its legacy :class:`Index` node.
"""

from __future__ import annotations

from cc import ast_nodes
from cc.lexer import tokenize
from cc.parser import Parser


def _expression_statement_place(source: str, /) -> ast_nodes.Node:
    """Return the place of the first body statement's load/store/incdec node."""
    program = Parser(tokenize(source)).parse_program()
    statement = program.functions[0].body[0]
    return getattr(statement, "place", statement)


def _return_expression(source: str, /) -> ast_nodes.Node:
    program = Parser(tokenize(source)).parse_program()
    statement = program.functions[0].body[0]
    return statement.value


def test_double_subscript_load_is_uniform_nested_subscript() -> None:
    """``return m[i][j];`` builds SubscriptPlace(SubscriptPlace(Variable, i), j)."""
    expression = _return_expression("int m[2][3]; int main(void){ return m[0][1]; }")
    assert isinstance(expression, ast_nodes.PlaceLoad)
    place = expression.place
    assert isinstance(place, ast_nodes.SubscriptPlace)
    assert isinstance(place.base, ast_nodes.SubscriptPlace)
    assert isinstance(place.base.base, ast_nodes.VariablePlace)
    assert place.base.base.name == "m"
    # No DereferencePlace anywhere in the uniform shape.
    assert not isinstance(place.base, ast_nodes.DereferencePlace)


def test_double_subscript_store_is_uniform_nested_subscript() -> None:
    """``m[i][j] = v;`` builds a uniform nested SubscriptPlace store."""
    place = _expression_statement_place("int m[2][3]; int main(void){ m[0][1] = 7; return 0; }")
    assert isinstance(place, ast_nodes.SubscriptPlace)
    assert isinstance(place.base, ast_nodes.SubscriptPlace)
    assert isinstance(place.base.base, ast_nodes.VariablePlace)
    assert place.base.base.name == "m"


def test_single_subscript_load_stays_index_node() -> None:
    """``return a[i];`` is unchanged — a plain Index node, not a Place."""
    expression = _return_expression("int a[4]; int main(void){ return a[2]; }")
    assert isinstance(expression, ast_nodes.Index)
    assert isinstance(expression.array, ast_nodes.Var)
    assert expression.array.name == "a"


def test_triple_subscript_load_is_uniform_nested_subscript() -> None:
    """``return c[i][j][k];`` builds a three-deep SubscriptPlace chain."""
    expression = _return_expression("int c[2][2][2]; int main(void){ return c[0][1][1]; }")
    assert isinstance(expression, ast_nodes.PlaceLoad)
    place = expression.place
    assert isinstance(place, ast_nodes.SubscriptPlace)
    assert isinstance(place.base, ast_nodes.SubscriptPlace)
    assert isinstance(place.base.base, ast_nodes.SubscriptPlace)
    assert isinstance(place.base.base.base, ast_nodes.VariablePlace)
    assert place.base.base.base.name == "c"
