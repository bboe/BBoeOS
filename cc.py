#!/usr/bin/env python3
"""Minimal C subset compiler for BBoeOS.

Compiles a tiny subset of C to NASM-compatible assembly that the BBoeOS
self-hosted assembler (or host NASM) can assemble into a flat binary.

v0 grammar:
    program   := func_decl*
    func_decl := 'void' IDENT '(' ')' '{' stmt* '}'
    stmt      := IDENT '(' args ')' ';'
    args      := STRING (',' STRING)*

v0 builtins:
    puts(STR)  -- prints STR verbatim (no auto-newline)

Usage: cc.py <input.c> [output.asm]
  Without output.asm, writes to stdout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

KEYWORDS = frozenset({"void"})

TOKEN_RE = re.compile(
    r"""
    (?P<WS>\s+)
  | (?P<LINE_COMMENT>//[^\n]*)
  | (?P<BLOCK_COMMENT>/\*[\s\S]*?\*/)
  | (?P<STRING>"(?:[^"\\]|\\.)*")
  | (?P<IDENT>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<LBRACE>\{)
  | (?P<RBRACE>\})
  | (?P<SEMI>;)
  | (?P<COMMA>,)
""",
    re.VERBOSE,
)


def lex(source: str) -> list[tuple[str, str, int]]:
    tokens: list[tuple[str, str, int]] = []
    pos = 0
    line = 1
    while pos < len(source):
        m = TOKEN_RE.match(source, pos)
        if not m:
            raise SyntaxError(f"line {line}: unexpected character {source[pos]!r}")
        kind = m.lastgroup
        assert kind is not None
        text = m.group()
        if kind in ("WS", "LINE_COMMENT", "BLOCK_COMMENT"):
            line += text.count("\n")
        else:
            if kind == "IDENT" and text in KEYWORDS:
                kind = text.upper()
            tokens.append((kind, text, line))
        pos = m.end()
    tokens.append(("EOF", "", line))
    return tokens


# ---------------------------------------------------------------------------
# Parser — recursive descent, returns a lightweight AST
# ---------------------------------------------------------------------------


class Parser:
    def __init__(self, tokens: list[tuple[str, str, int]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str, int]:
        return self.tokens[self.pos]

    def eat(self, kind: str | None = None) -> tuple[str, str, int]:
        tok = self.tokens[self.pos]
        if kind is not None and tok[0] != kind:
            raise SyntaxError(
                f"line {tok[2]}: expected {kind}, got {tok[0]} ({tok[1]!r})"
            )
        self.pos += 1
        return tok

    def parse_program(self) -> tuple[str, list]:
        funcs = []
        while self.peek()[0] != "EOF":
            funcs.append(self.parse_func())
        return ("program", funcs)

    def parse_func(self) -> tuple:
        self.eat("VOID")
        name = self.eat("IDENT")[1]
        self.eat("LPAREN")
        self.eat("RPAREN")
        self.eat("LBRACE")
        body: list[tuple] = []
        while self.peek()[0] != "RBRACE":
            body.append(self.parse_stmt())
        self.eat("RBRACE")
        return ("func", name, body)

    def parse_stmt(self) -> tuple:
        name = self.eat("IDENT")[1]
        self.eat("LPAREN")
        args: list[tuple] = []
        if self.peek()[0] != "RPAREN":
            args.append(self.parse_expr())
            while self.peek()[0] == "COMMA":
                self.eat("COMMA")
                args.append(self.parse_expr())
        self.eat("RPAREN")
        self.eat("SEMI")
        return ("call", name, args)

    def parse_expr(self) -> tuple:
        tok = self.eat("STRING")
        # Strip surrounding quotes; inner content (with C escapes) is
        # passed through to asm backtick strings, which share the same
        # escape rules (\n, \t, \0, \\).
        return ("string", tok[1][1:-1])


# ---------------------------------------------------------------------------
# Code generation — walk the AST, emit NASM-compatible assembly
# ---------------------------------------------------------------------------


class CodeGen:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.strings: list[tuple[str, str]] = []

    def emit(self, line: str = "") -> None:
        self.lines.append(line)

    def gen(self, ast: tuple) -> str:
        self.emit("        org 0600h")
        self.emit("")
        self.emit('%include "constants.asm"')
        self.emit("")
        for func in ast[1]:
            self.gen_func(func)
        if self.strings:
            self.emit(";; --- string literals ---")
            for label, content in self.strings:
                self.emit(f"{label}: db `{content}\\0`")
        return "\n".join(self.lines) + "\n"

    def gen_func(self, func: tuple) -> None:
        _, name, body = func
        self.emit(f"{name}:")
        for stmt in body:
            self.gen_stmt(stmt)
        if name == "main":
            self.emit("        mov ah, SYS_EXIT")
            self.emit("        int 30h")
        else:
            self.emit("        ret")
        self.emit("")

    def gen_stmt(self, stmt: tuple) -> None:
        if stmt[0] != "call":
            raise SyntaxError(f"unknown statement kind: {stmt[0]}")
        _, name, args = stmt
        handler = getattr(self, f"builtin_{name}", None)
        if handler is None:
            raise SyntaxError(f"unknown builtin: {name}")
        handler(args)

    def builtin_puts(self, args: list[tuple]) -> None:
        if len(args) != 1 or args[0][0] != "string":
            raise SyntaxError("puts() expects exactly one string argument")
        content = args[0][1]
        label = f"_str_{len(self.strings)}"
        self.strings.append((label, content))
        self.emit(f"        mov si, {label}")
        self.emit("        mov ah, SYS_IO_PUTS")
        self.emit("        int 30h")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: cc.py <input.c> [output.asm]", file=sys.stderr)
        return 1

    source = Path(sys.argv[1]).read_text()
    tokens = lex(source)
    ast = Parser(tokens).parse_program()
    output = CodeGen().gen(ast)

    if len(sys.argv) == 3:
        Path(sys.argv[2]).write_text(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
