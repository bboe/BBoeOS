"""Parser acceptance of multidimensional array declarators.

The parser historically caps array declarators at a single ``[N]`` bracket,
rejecting ``int m[2][3]``.  These tests pin the lifted behavior: every
declarator position records the full outer-to-inner list of dimension
expressions on ``ArrayDecl.dimensions``, while a plain single-dimension
array keeps ``dimensions is None`` so the legacy codegen path is byte-identical.
"""

from __future__ import annotations

from cc import ast_nodes
from cc.lexer import tokenize
from cc.parser import Parser


def _first_body_statement(source: str, /) -> ast_nodes.Node:
    program = _parse(source)
    return program.functions[0].body[0]


def _only_global(source: str, /) -> ast_nodes.ArrayDecl:
    program = _parse(source)
    assert len(program.globals) == 1
    declaration = program.globals[0]
    assert isinstance(declaration, ast_nodes.ArrayDecl)
    return declaration


def _only_param(source: str, /) -> ast_nodes.Param:
    program = _parse(source)
    parameters = program.functions[0].params
    assert len(parameters) == 1
    return parameters[0]


def _only_struct_field(source: str, /) -> ast_nodes.StructField:
    program = _parse(source)
    declaration = program.globals[0]
    assert isinstance(declaration, ast_nodes.StructDecl)
    assert len(declaration.fields) == 1
    return declaration.fields[0]


def _parse(source: str, /) -> ast_nodes.Program:
    return Parser(tokenize(source)).parse_program()


def test_empty_bracket_param_stays_legacy() -> None:
    """A plain ``int argv[]`` param keeps is_array with no recorded dimensions."""
    param = _only_param("int main(int m[]) { return 0; }")
    assert param.is_array is True
    assert param.dimensions is None


def test_single_dimension_global_leaves_dimensions_none() -> None:
    """A plain ``int a[4];`` keeps the legacy single-``size`` shape untouched."""
    declaration = _only_global("int a[4];")
    assert declaration.size == ast_nodes.Int(value=4)
    assert declaration.dimensions is None


def test_single_dimension_local_leaves_dimensions_none() -> None:
    """A plain local ``int a[4];`` keeps the legacy single-``size`` shape."""
    statement = _first_body_statement("int main(void) { int a[4]; return 0; }")
    assert isinstance(statement, ast_nodes.ArrayDecl)
    assert statement.dimensions is None


def test_single_dimension_struct_field_unchanged() -> None:
    """A plain ``char buffer[15];`` field keeps the legacy ``T[N]`` flat form."""
    field = _only_struct_field("struct s { char buffer[15]; };")
    assert field.type_name == "char[15]"


def test_three_dimension_global_records_all_dimensions() -> None:
    """``int c[2][3][4];`` records all three bracket sizes."""
    declaration = _only_global("int c[2][3][4];")
    assert declaration.dimensions == [
        ast_nodes.Int(value=2),
        ast_nodes.Int(value=3),
        ast_nodes.Int(value=4),
    ]


def test_three_dimension_struct_field_bakes_all_brackets() -> None:
    """A 3-D struct field bakes every bracket into the flat form."""
    field = _only_struct_field("struct s { int c[2][3][4]; };")
    assert field.type_name == "int[2][3][4]"


def test_two_dimension_global_keeps_outer_in_size() -> None:
    """The outermost dimension still populates the legacy ``size`` field."""
    declaration = _only_global("int m[2][3];")
    assert declaration.size == ast_nodes.Int(value=2)


def test_two_dimension_global_records_both_dimensions() -> None:
    """``int m[2][3];`` records both bracket sizes outer-to-inner."""
    declaration = _only_global("int m[2][3];")
    assert declaration.dimensions == [ast_nodes.Int(value=2), ast_nodes.Int(value=3)]


def test_two_dimension_local_records_both_dimensions() -> None:
    """A function-local ``int m[2][3];`` records both dimensions too."""
    statement = _first_body_statement("int main(void) { int m[2][3]; return 0; }")
    assert isinstance(statement, ast_nodes.ArrayDecl)
    assert statement.dimensions == [ast_nodes.Int(value=2), ast_nodes.Int(value=3)]


def test_two_dimension_param_records_dimensions() -> None:
    """``int m[2][3]`` param records both dimensions and marks is_array."""
    param = _only_param("int main(int m[2][3]) { return 0; }")
    assert param.is_array is True
    assert param.dimensions == [ast_nodes.Int(value=2), ast_nodes.Int(value=3)]


def test_two_dimension_struct_field_bakes_both_brackets() -> None:
    """``int m[2][3];`` field bakes the full multidimensional flat form."""
    field = _only_struct_field("struct s { int m[2][3]; };")
    assert field.type_name == "int[2][3]"


def test_unsized_outer_dimension_param_records_dimensions() -> None:
    """``int m[][3]`` (unsized outer, C decay form) records [None, Int(3)]."""
    param = _only_param("int main(int m[][3]) { return 0; }")
    assert param.dimensions == [None, ast_nodes.Int(value=3)]
