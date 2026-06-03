"""Parser acceptance of pointer-to-array declarators.

``int (*p)[3]`` declares a pointer to an array of 3 ints.  These tests
pin the lifted behavior: every declarator position records the pointee
array dimensions in ``pointer_array_dimensions``, while the existing
function-pointer / array-of-function-pointer / plain array-of-pointers
paths remain byte-identical.
"""

from __future__ import annotations

from cc import ast_nodes
from cc.lexer import tokenize
from cc.parser import Parser


def _first_body_statement(source: str, /) -> ast_nodes.Node:
    program = _parse(source)
    return program.functions[0].body[0]


def _only_global(source: str, /) -> ast_nodes.Node:
    program = _parse(source)
    assert len(program.globals) == 1
    return program.globals[0]


def _only_param(source: str, /) -> ast_nodes.Param:
    program = _parse(source)
    parameters = program.functions[0].params
    assert len(parameters) == 1
    return parameters[0]


def _parse(source: str, /) -> ast_nodes.Program:
    return Parser(tokenize(source)).parse_program()


def test_array_of_pointers_unchanged() -> None:
    """``int *q[3];`` still parses as an ArrayDecl — unchanged."""
    declaration = _only_global("int *q[3];")
    assert isinstance(declaration, ast_nodes.ArrayDecl)
    assert declaration.name == "q"


def test_function_pointer_local_unchanged() -> None:
    """``int (*fp)(int);`` still parses as a function-pointer VarDecl."""
    statement = _first_body_statement("int main(void) { int (*fp)(int x); return 0; }")
    assert isinstance(statement, ast_nodes.VarDecl)
    assert statement.type_name == "function_pointer"
    assert statement.function_pointer_params is not None
    assert not hasattr(statement, "pointer_array_dimensions") or statement.pointer_array_dimensions is None


def test_global_pointer_to_array_single_dimension() -> None:
    """Global ``int (*p)[3];`` parses as VarDecl with pointer_array_dimensions=[Int(3)]."""
    declaration = _only_global("int (*p)[3];")
    assert isinstance(declaration, ast_nodes.VarDecl)
    assert declaration.name == "p"
    assert declaration.type_name == "int"
    assert declaration.pointer_array_dimensions == [ast_nodes.Int(value=3)]


def test_global_pointer_to_multidim_array() -> None:
    """Global ``int (*p)[3][4];`` produces pointer_array_dimensions=[Int(3), Int(4)]."""
    declaration = _only_global("int (*p)[3][4];")
    assert isinstance(declaration, ast_nodes.VarDecl)
    assert declaration.name == "p"
    assert declaration.type_name == "int"
    assert declaration.pointer_array_dimensions == [
        ast_nodes.Int(value=3),
        ast_nodes.Int(value=4),
    ]


def test_local_pointer_to_array_single_dimension() -> None:
    """Local ``int (*p)[3];`` parses as VarDecl with pointer_array_dimensions=[Int(3)]."""
    statement = _first_body_statement("int main(void) { int (*p)[3]; return 0; }")
    assert isinstance(statement, ast_nodes.VarDecl)
    assert statement.name == "p"
    assert statement.type_name == "int"
    assert statement.pointer_array_dimensions == [ast_nodes.Int(value=3)]


def test_local_pointer_to_multidim_array() -> None:
    """Local ``int (*p)[3][4];`` produces pointer_array_dimensions=[Int(3), Int(4)]."""
    statement = _first_body_statement("int main(void) { int (*p)[3][4]; return 0; }")
    assert isinstance(statement, ast_nodes.VarDecl)
    assert statement.pointer_array_dimensions == [
        ast_nodes.Int(value=3),
        ast_nodes.Int(value=4),
    ]


def test_param_pointer_to_array() -> None:
    """Parameter ``int (*p)[3]`` records pointer_array_dimensions=[Int(3)]."""
    parameter = _only_param("int f(int (*p)[3]){ return 0; }")
    assert parameter.name == "p"
    assert parameter.type == "int"
    assert parameter.pointer_array_dimensions == [ast_nodes.Int(value=3)]
