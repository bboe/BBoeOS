#!/usr/bin/env python3
"""Minimal C subset compiler for BBoeOS.

Compiles a tiny subset of C to NASM-compatible assembly that the BBoeOS
self-hosted assembler (or host NASM) can assemble into a flat binary.

v0 grammar:
    program   := function_declaration*
    function_declaration := 'void' IDENT '(' ')' '{' statement* '}'
    statement := IDENT '(' arguments ')' ';'
    arguments := STRING (',' STRING)*

v0 builtins:
    puts(STR)  -- prints STR verbatim (no auto-newline)

Usage: cc.py <input.c> [output.asm]
  Without output.asm, writes to stdout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

KEYWORDS = frozenset({"void"})

TOKEN_PATTERN = re.compile(
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


def tokenize(source: str, /) -> list[tuple[str, str, int]]:
    """Tokenize C source code into a list of (kind, text, line) tuples."""
    tokens: list[tuple[str, str, int]] = []
    position = 0
    line = 1
    while position < len(source):
        match = TOKEN_PATTERN.match(source, position)
        if not match:
            message = f"line {line}: unexpected character {source[position]!r}"
            raise SyntaxError(message)
        kind = match.lastgroup
        assert kind is not None
        text = match.group()
        if kind in {"WS", "LINE_COMMENT", "BLOCK_COMMENT"}:
            line += text.count("\n")
        else:
            if kind == "IDENT" and text in KEYWORDS:
                kind = text.upper()
            tokens.append((kind, text, line))
        position = match.end()
    tokens.append(("EOF", "", line))
    return tokens


class Parser:
    """Recursive descent parser that produces a lightweight AST."""

    def __init__(self, tokens: list[tuple[str, str, int]]) -> None:
        """Initialize parser with token list."""
        self.tokens = tokens
        self.position = 0

    def peek(self) -> tuple[str, str, int]:
        """Return the current token without consuming it."""
        return self.tokens[self.position]

    def eat(self, kind: str | None = None) -> tuple[str, str, int]:
        """Consume and return the next token, optionally asserting its kind."""
        token = self.tokens[self.position]
        if kind is not None and token[0] != kind:
            message = f"line {token[2]}: expected {kind}, got {token[0]} ({token[1]!r})"
            raise SyntaxError(
                message,
            )
        self.position += 1
        return token

    def parse_program(self) -> tuple[str, list]:
        """Parse the full program."""
        functions = []
        while self.peek()[0] != "EOF":
            functions.append(self.parse_function())
        return ("program", functions)

    def parse_function(self) -> tuple:
        """Parse a function declaration."""
        self.eat("VOID")
        name = self.eat("IDENT")[1]
        self.eat("LPAREN")
        self.eat("RPAREN")
        self.eat("LBRACE")
        body: list[tuple] = []
        while self.peek()[0] != "RBRACE":
            body.append(self.parse_statement())
        self.eat("RBRACE")
        return ("function", name, body)

    def parse_statement(self) -> tuple:
        """Parse a statement (currently only function calls)."""
        name = self.eat("IDENT")[1]
        self.eat("LPAREN")
        arguments: list[tuple] = []
        if self.peek()[0] != "RPAREN":
            arguments.append(self.parse_expression())
            while self.peek()[0] == "COMMA":
                self.eat("COMMA")
                arguments.append(self.parse_expression())
        self.eat("RPAREN")
        self.eat("SEMI")
        return ("call", name, arguments)

    def parse_expression(self) -> tuple:
        """Parse an expression (currently only string literals)."""
        token = self.eat("STRING")
        # Strip surrounding quotes; inner content (with C escapes) is
        # passed through to asm backtick strings, which share the same
        # escape rules (\n, \t, \0, \\).
        return ("string", token[1][1:-1])


class CodeGenerator:
    """Generate NASM assembly from the parsed AST."""

    def __init__(self) -> None:
        """Initialize code generator state."""
        self.lines: list[str] = []
        self.strings: list[tuple[str, str]] = []

    def emit(self, line: str = "") -> None:
        """Append an assembly line to the output."""
        self.lines.append(line)

    def generate(self, ast: tuple) -> str:
        """Generate assembly from the full program AST."""
        self.emit("        org 0600h")
        self.emit("")
        self.emit('%include "constants.asm"')
        self.emit("")
        for function in ast[1]:
            self.generate_function(function)
        if self.strings:
            self.emit(";; --- string literals ---")
            for label, content in self.strings:
                self.emit(f"{label}: db `{content}\\0`")
        return "\n".join(self.lines) + "\n"

    def generate_function(self, function: tuple) -> None:
        """Generate assembly for a single function."""
        _, name, body = function
        self.emit(f"{name}:")
        for statement in body:
            self.generate_statement(statement)
        if name == "main":
            self.emit("        mov ah, SYS_EXIT")
            self.emit("        int 30h")
        else:
            self.emit("        ret")
        self.emit("")

    def generate_statement(self, statement: tuple) -> None:
        """Generate assembly for a single statement."""
        if statement[0] != "call":
            message = f"unknown statement kind: {statement[0]}"
            raise SyntaxError(message)
        _, name, arguments = statement
        handler = getattr(self, f"builtin_{name}", None)
        if handler is None:
            message = f"unknown builtin: {name}"
            raise SyntaxError(message)
        handler(arguments)

    def builtin_puts(self, arguments: list[tuple]) -> None:
        """Emit assembly for the puts() builtin."""
        if len(arguments) != 1 or arguments[0][0] != "string":
            message = "puts() expects exactly one string argument"
            raise SyntaxError(message)
        content = arguments[0][1]
        label = f"_str_{len(self.strings)}"
        self.strings.append((label, content))
        self.emit(f"        mov si, {label}")
        self.emit("        mov ah, SYS_IO_PUTS")
        self.emit("        int 30h")


def main() -> int:
    """Compile source to assembly and write to stdout or a file."""
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: cc.py <input.c> [output.asm]", file=sys.stderr)
        return 1

    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    tokens = tokenize(source)
    ast = Parser(tokens).parse_program()
    output = CodeGenerator().generate(ast)

    if len(sys.argv) == 3:
        Path(sys.argv[2]).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
