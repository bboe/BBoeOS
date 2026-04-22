"""Minimal C subset compiler for BBoeOS.

The public API is :func:`cc.cli.main` (CLI entry point) and
:class:`cc.errors.CompileError`.  Individual phases live in
:mod:`cc.lexer`, :mod:`cc.preprocessor`, :mod:`cc.parser`,
:mod:`cc.ir`, and :mod:`cc.codegen`; AST node types are in
:mod:`cc.ast_nodes`; the codegen target abstraction is in
:mod:`cc.target`.
"""

from cc.errors import CompileError

__all__ = ["CompileError"]
