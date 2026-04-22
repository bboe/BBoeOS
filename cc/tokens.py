"""Lexer / parser token tables.

Constants shared between :mod:`cc.lexer` and :mod:`cc.parser`.  Codegen-
and IR-specific constants live alongside their consumers (codegen jump
tables in :mod:`cc.codegen`; IR comparison sets in :mod:`cc.ir`).
"""

from __future__ import annotations

import re

ADDITIVE_OPERATORS = frozenset({"MINUS", "PLUS"})

#: Comparison operators as source strings (not token kinds).  Shared
#: between the parser (deciding when to wrap a bare expression in
#: ``!= 0``) and the IR builder (deciding which BinaryOperation nodes
#: lower to :class:`cc.ir.IRBranchFalse`).
COMPARISON_OPERATIONS = frozenset({"==", "!=", "<", "<=", ">", ">="})

#: Invert a comparison operator (for ``_build_cond_true`` via :class:`cc.ir.IRBranchFalse`).
INVERT_COMPARISON = {"==": "!=", "!=": "==", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}

CHARACTER_ESCAPES = {
    '"': 0x22,
    "'": 0x27,
    "0": 0x00,
    "\\": 0x5C,
    "b": 0x08,
    "e": 0x1B,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
}

COMPARISON_OPERATORS = frozenset({"EQ", "GE", "GT", "LE", "LT", "NE"})

KEYWORDS = frozenset({
    "break",
    "char",
    "const",
    "continue",
    "do",
    "else",
    "if",
    "int",
    "long",
    "return",
    "sizeof",
    "uint8_t",
    "unsigned",
    "void",
    "while",
})

MULTIPLICATIVE_OPERATORS = frozenset({"PERCENT", "SLASH", "STAR"})

SHIFT_OPERATORS = frozenset({"SHL", "SHR"})

COMPOUND_ASSIGN_OPERATORS = {
    "AMP_ASSIGN": "&",
    "CARET_ASSIGN": "^",
    "PIPE_ASSIGN": "|",
    "PLUS_ASSIGN": "+",
    "SHL_ASSIGN": "<<",
    "SHR_ASSIGN": ">>",
}

TOKEN_PATTERN = re.compile(
    r"""
    (?P<WS>\s+)
  | (?P<BLOCK_COMMENT>/\*[\s\S]*?\*/)
  | (?P<LINE_COMMENT>//[^\n]*)
  | (?P<CHAR_LIT>'(?:[^'\\]|\\x[0-9a-fA-F]{1,2}|\\.)')
  | (?P<IDENT>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<NUMBER>0[xX][0-9a-fA-F]+|[0-9]+)
  | (?P<STRING>"(?:[^"\\]|\\.)*")
  | (?P<EQ>==)
  | (?P<GE>>=)
  | (?P<SHR_ASSIGN>>>=)
  | (?P<SHR>>>)
  | (?P<LE><=)
  | (?P<SHL_ASSIGN><<=)
  | (?P<SHL><<)
  | (?P<NE>!=)
  | (?P<PLUS_ASSIGN>\+=)
  | (?P<ASSIGN>=)
  | (?P<GT>>)
  | (?P<LT><)
  | (?P<MINUS>-)
  | (?P<AND_AND>&&)
  | (?P<AMP_ASSIGN>&=)
  | (?P<AMP>&)
  | (?P<OR_OR>\|\|)
  | (?P<PIPE_ASSIGN>\|=)
  | (?P<PIPE>\|)
  | (?P<CARET_ASSIGN>\^=)
  | (?P<CARET>\^)
  | (?P<TILDE>~)
  | (?P<NOT>!)
  | (?P<PERCENT>%)
  | (?P<PLUS>\+)
  | (?P<SLASH>/)
  | (?P<STAR>\*)
  | (?P<LBRACE>\{)
  | (?P<LBRACKET>\[)
  | (?P<LPAREN>\()
  | (?P<RBRACE>\})
  | (?P<RBRACKET>\])
  | (?P<RPAREN>\))
  | (?P<COMMA>,)
  | (?P<SEMI>;)
""",
    re.VERBOSE,
)

TYPE_TOKENS = frozenset({"CHAR", "CONST", "INT", "LONG", "UINT8_T", "UNSIGNED", "VOID"})
