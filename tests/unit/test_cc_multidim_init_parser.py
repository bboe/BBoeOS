"""Parser acceptance of nested and flat multidimensional array initializers.

These tests pin the behavior introduced by ``_parse_multidim_array_init``:
an ``ArrayDecl.init`` for a multidimensional (2+ ``[N]`` brackets) non-struct
element type is an ``ArrayInit`` whose ``elements`` may themselves be
``ArrayInit`` nodes (nested braces) or flat expression leaves.
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


def _parse(source: str, /) -> ast_nodes.Program:
    return Parser(tokenize(source)).parse_program()


def test_flat_multidim_global() -> None:
    """``int m[2][3] = {1,2,3,4,5,6};`` → ArrayInit with 6 Int leaves."""
    declaration = _only_global("int m[2][3] = {1,2,3,4,5,6};")
    initializer = declaration.init
    assert isinstance(initializer, ast_nodes.ArrayInit)
    assert len(initializer.elements) == 6
    assert all(isinstance(element, ast_nodes.Int) for element in initializer.elements)
    assert initializer.elements[0] == ast_nodes.Int(value=1)
    assert initializer.elements[5] == ast_nodes.Int(value=6)


def test_local_multidim_nested() -> None:
    """Local ``int m[2][2] = {{1,2},{3,4}};`` → nested ArrayInit shape."""
    declaration = _first_body_statement("int main(void){ int m[2][2] = {{1,2},{3,4}}; return 0; }")
    assert isinstance(declaration, ast_nodes.ArrayDecl)
    initializer = declaration.init
    assert isinstance(initializer, ast_nodes.ArrayInit)
    assert len(initializer.elements) == 2
    first_row = initializer.elements[0]
    assert isinstance(first_row, ast_nodes.ArrayInit)
    assert first_row.elements[0] == ast_nodes.Int(value=1)
    assert first_row.elements[1] == ast_nodes.Int(value=2)
    second_row = initializer.elements[1]
    assert isinstance(second_row, ast_nodes.ArrayInit)
    assert second_row.elements[0] == ast_nodes.Int(value=3)
    assert second_row.elements[1] == ast_nodes.Int(value=4)


def test_nested_multidim_global() -> None:
    """``int m[2][3] = {{1,2,3},{4,5,6}};`` → 2 ArrayInit rows of 3 Ints."""
    declaration = _only_global("int m[2][3] = {{1,2,3},{4,5,6}};")
    initializer = declaration.init
    assert isinstance(initializer, ast_nodes.ArrayInit)
    assert len(initializer.elements) == 2
    first_row = initializer.elements[0]
    assert isinstance(first_row, ast_nodes.ArrayInit)
    assert len(first_row.elements) == 3
    assert first_row.elements[1] == ast_nodes.Int(value=2)
    second_row = initializer.elements[1]
    assert isinstance(second_row, ast_nodes.ArrayInit)
    assert len(second_row.elements) == 3
    assert second_row.elements[2] == ast_nodes.Int(value=6)


def test_single_dim_array_unchanged() -> None:
    """``int a[3] = {1,2,3};`` still uses the old flat ArrayInit path."""
    declaration = _only_global("int a[3] = {1,2,3};")
    initializer = declaration.init
    assert isinstance(initializer, ast_nodes.ArrayInit)
    assert len(initializer.elements) == 3
    assert initializer.elements[0] == ast_nodes.Int(value=1)
    assert initializer.elements[2] == ast_nodes.Int(value=3)


def test_single_element_multidim_global() -> None:
    """``int m[2][3] = {0};`` → ArrayInit with a single Int(0)."""
    declaration = _only_global("int m[2][3] = {0};")
    initializer = declaration.init
    assert isinstance(initializer, ast_nodes.ArrayInit)
    assert len(initializer.elements) == 1
    assert initializer.elements[0] == ast_nodes.Int(value=0)


def test_three_dimensional_nested() -> None:
    """``int c[2][2][2] = {{{1,2},{3,4}},{{5,6},{7,8}}};`` → three levels."""
    declaration = _only_global("int c[2][2][2] = {{{1,2},{3,4}},{{5,6},{7,8}}};")
    initializer = declaration.init
    assert isinstance(initializer, ast_nodes.ArrayInit)
    assert len(initializer.elements) == 2
    outer_first = initializer.elements[0]
    assert isinstance(outer_first, ast_nodes.ArrayInit)
    assert len(outer_first.elements) == 2
    inner_first = outer_first.elements[0]
    assert isinstance(inner_first, ast_nodes.ArrayInit)
    assert inner_first.elements[0] == ast_nodes.Int(value=1)
    assert inner_first.elements[1] == ast_nodes.Int(value=2)
    outer_second = initializer.elements[1]
    assert isinstance(outer_second, ast_nodes.ArrayInit)
    inner_last = outer_second.elements[1]
    assert isinstance(inner_last, ast_nodes.ArrayInit)
    assert inner_last.elements[0] == ast_nodes.Int(value=7)
    assert inner_last.elements[1] == ast_nodes.Int(value=8)
