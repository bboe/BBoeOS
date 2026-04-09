#!/usr/bin/env python3
"""Minimal C subset compiler for BBoeOS.

Compiles a tiny subset of C to NASM-compatible assembly that the BBoeOS
self-hosted assembler (or host NASM) can assemble into a flat binary.

Grammar:
    program              := function_declaration*
    function_declaration := type IDENT '(' ')' '{' statement* '}'
    type                 := 'void' | 'int' | 'char' '*'
    statement            := variable_declaration | assign_statement | while_statement
                          | call_statement
    variable_declaration             := type IDENT ('[' ']')? '=' (expression
                          | '{' expression_list '}') ';'
    assign               := IDENT '=' expression ';'
                          |  IDENT '+=' expression ';'
    while                := 'while' '(' expression ')' '{' statement* '}'
    call_statement       := IDENT '(' arguments ')' ';'
    arguments            := expression (',' expression)*
    expression           := comparison_expression
    comparison_expression := additive_expression
                           (('<'|'>'|'<='|'>='|'=='|'!=')
                            additive_expression)?
    additive_expression  := multiplicative_expression
                           (('+'|'-') multiplicative_expression)*
    multiplicative_expression := primary (('*'|'/') primary)*
    primary              := NUMBER | STRING | sizeof
                          | IDENT ('[' expression ']')? | '(' expression ')'

Builtins:
    puts(expression)  -- print string (no auto-newline)
    putc(expression)  -- print single character

Usage: cc.py <input.c> [output.asm]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ADDITIVE_OPERATORS = frozenset({"MINUS", "PLUS"})

CHARACTER_ESCAPES = {
    "0": 0x00,
    "\\": 0x5C,
    '"': 0x22,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
}

COMPARISON_OPERATORS = frozenset({"EQ", "GE", "GT", "LE", "LT", "NE"})

KEYWORDS = frozenset({"char", "if", "int", "sizeof", "void", "while"})

JUMP_WHEN_FALSE = {
    "!=": "je",
    "<": "jge",
    "<=": "jg",
    ">": "jle",
    ">=": "jl",
    "==": "jne",
}

TOKEN_PATTERN = re.compile(
    r"""
    (?P<WS>\s+)
  | (?P<BLOCK_COMMENT>/\*[\s\S]*?\*/)
  | (?P<LINE_COMMENT>//[^\n]*)
  | (?P<CHAR_LIT>'(?:[^'\\]|\\.)')
  | (?P<IDENT>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<NUMBER>0[xX][0-9a-fA-F]+|[0-9]+)
  | (?P<STRING>"(?:[^"\\]|\\.)*")
  | (?P<EQ>==)
  | (?P<GE>>=)
  | (?P<LE><=)
  | (?P<NE>!=)
  | (?P<PLUS_ASSIGN>\+=)
  | (?P<ASSIGN>=)
  | (?P<GT>>)
  | (?P<LT><)
  | (?P<MINUS>-)
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

MULTIPLICATIVE_OPERATORS = frozenset({"SLASH", "STAR"})

TYPE_TOKENS = frozenset({"CHAR", "INT", "VOID"})


class CodeGenerator:
    """Generates NASM x86 assembly from the parsed AST."""

    def __init__(self) -> None:
        """Initialize code generator state."""
        self.array_sizes: dict[str, int] = {}
        self.arrays: list[tuple[str, list[str]]] = []
        self.frame_size: int = 0
        self.label_id: int = 0
        self.lines: list[str] = []
        self.locals: dict[str, int] = {}
        self.needs_argv_buf: bool = False
        self.strings: list[tuple[str, str]] = []

    def allocate_local(self, name: str, /) -> int:
        """Allocate a 2-byte local variable on the stack frame.

        Returns:
            The current frame size after allocation.

        """
        self.frame_size += 2
        self.locals[name] = self.frame_size
        return self.frame_size

    def builtin_putc(self, arguments: list[tuple], /) -> None:
        """Generate code for the putc() builtin.

        Raises:
            SyntaxError: If the wrong number of arguments is provided.

        """
        if len(arguments) != 1:
            message = "putc() expects exactly one argument"
            raise SyntaxError(message)
        argument = arguments[0]
        if argument[0] == "string":
            byte_val = decode_first_character(argument[1])
            self.emit(f"        mov al, {byte_val}")
        else:
            self.generate_expression(argument)
        self.emit("        mov ah, SYS_IO_PUTC")
        self.emit("        int 30h")

    def builtin_puts(self, arguments: list[tuple], /) -> None:
        """Generate code for the puts() builtin.

        Raises:
            SyntaxError: If the wrong number of arguments is provided.

        """
        if len(arguments) != 1:
            message = "puts() expects exactly one argument"
            raise SyntaxError(message)
        argument = arguments[0]
        if argument[0] == "string":
            label = f"_str_{len(self.strings)}"
            self.strings.append((label, argument[1]))
            self.emit(f"        mov si, {label}")
        else:
            self.generate_expression(argument)
            self.emit("        mov si, ax")
        self.emit("        mov ah, SYS_IO_PUTS")
        self.emit("        int 30h")

    def emit(self, line: str = "") -> None:
        """Append a line of assembly to the output buffer."""
        self.lines.append(line)

    def emit_argument_vector_startup(self, parameters: list[tuple], /) -> None:
        """Emit inline startup code that parses EXEC_ARG into argc/argv."""
        argc_name = None
        argv_name = None
        for type_string, pname, is_array in parameters:
            if is_array:
                argv_name = pname
            elif argc_name is None:
                argc_name = pname
        if not argv_name:
            return

        self.needs_argv_buf = True
        label_index = self.new_label()

        self.emit("        cld")
        self.emit("        xor cx, cx")
        self.emit("        mov si, [EXEC_ARG]")
        self.emit("        test si, si")
        self.emit(f"        jz .args_done_{label_index}")
        self.emit("        mov di, _argv")
        self.emit(f".scan_{label_index}:")
        self.emit("        cmp byte [si], ' '")
        self.emit(f"        jne .check_{label_index}")
        self.emit("        inc si")
        self.emit(f"        jmp .scan_{label_index}")
        self.emit(f".check_{label_index}:")
        self.emit("        cmp byte [si], 0")
        self.emit(f"        je .args_done_{label_index}")
        self.emit("        mov [di], si")
        self.emit("        add di, 2")
        self.emit("        inc cx")
        self.emit(f".end_{label_index}:")
        self.emit("        cmp byte [si], 0")
        self.emit(f"        je .args_done_{label_index}")
        self.emit("        cmp byte [si], ' '")
        self.emit(f"        je .term_{label_index}")
        self.emit("        inc si")
        self.emit(f"        jmp .end_{label_index}")
        self.emit(f".term_{label_index}:")
        self.emit("        mov byte [si], 0")
        self.emit("        inc si")
        self.emit(f"        jmp .scan_{label_index}")
        self.emit(f".args_done_{label_index}:")
        if argc_name:
            self.emit(f"        mov [bp-{self.locals[argc_name]}], cx")
        self.emit(f"        mov word [bp-{self.locals[argv_name]}], _argv")

    def generate(self, ast: tuple, /) -> str:
        """Generate assembly for an entire program AST.

        Returns:
            The complete assembly source as a string.

        """
        self.emit("        org 0600h")
        self.emit()
        self.emit('%include "constants.asm"')
        self.emit()
        for function in ast[1]:
            self.generate_function(function)
        if self.strings:
            self.emit(";; --- string literals ---")
            for label, content in self.strings:
                self.emit(f"{label}: db `{content}\\0`")
        if self.arrays:
            self.emit(";; --- array data ---")
            for label, elems in self.arrays:
                self.emit(f"{label}: dw {', '.join(elems)}")
        if self.needs_argv_buf:
            self.emit("_argv: times 32 db 0")
        return "\n".join(self.lines) + "\n"

    def generate_call(self, statement: tuple, /) -> None:
        """Generate code for a function call statement.

        Raises:
            SyntaxError: If the called function is not a known builtin.

        """
        _, name, arguments = statement
        handler = getattr(self, f"builtin_{name}", None)
        if handler is None:
            message = f"unknown builtin: {name}"
            raise SyntaxError(message)
        handler(arguments)

    def generate_expression(self, expression: tuple, /) -> None:
        """Generate code for an expression, leaving the result in AX.

        Raises:
            SyntaxError: If an unknown expression kind or operator is encountered.

        """
        kind = expression[0]
        if kind == "int":
            self.emit(f"        mov ax, {expression[1]}")
        elif kind == "string":
            label = f"_str_{len(self.strings)}"
            self.strings.append((label, expression[1]))
            self.emit(f"        mov ax, {label}")
        elif kind == "variable":
            vname = expression[1]
            if vname not in self.locals:
                message = f"undefined variable: {vname}"
                raise SyntaxError(message)
            self.emit(f"        mov ax, [bp-{self.locals[vname]}]")
        elif kind == "index":
            _, vname, index_expression = expression
            if vname not in self.locals:
                message = f"undefined variable: {vname}"
                raise SyntaxError(message)
            self.emit(f"        mov bx, [bp-{self.locals[vname]}]")
            self.emit("        push bx")
            self.generate_expression(index_expression)
            self.emit("        add ax, ax")
            self.emit("        pop bx")
            self.emit("        add bx, ax")
            self.emit("        mov ax, [bx]")
        elif kind == "sizeof_type":
            type_sizes = {"char": 1, "char*": 2, "int": 2, "void": 0}
            self.emit(f"        mov ax, {type_sizes.get(expression[1], 2)}")
        elif kind == "sizeof_variable":
            vname = expression[1]
            if vname in self.array_sizes:
                size = self.array_sizes[vname] * 2  # word-sized elements
            else:
                size = 2  # all non-array variables are word-sized
            self.emit(f"        mov ax, {size}")
        elif kind == "binary_operator":
            _, operator, left, right = expression
            self.generate_expression(left)
            self.emit("        push ax")
            self.generate_expression(right)
            self.emit("        pop cx")  # CX = left, AX = right
            if operator == "+":
                self.emit("        add ax, cx")
            elif operator == "-":
                self.emit("        sub cx, ax")
                self.emit("        mov ax, cx")
            elif operator == "*":
                self.emit("        mul cx")
            elif operator == "/":
                # CX=left, AX=right — swap so AX=dividend, CX=divisor
                self.emit("        xchg ax, cx")
                self.emit("        xor dx, dx")
                self.emit("        div cx")
            elif operator in JUMP_WHEN_FALSE:
                self.emit("        cmp cx, ax")
                self.emit("        mov ax, 0")
            else:
                message = f"unknown operator: {operator}"
                raise SyntaxError(message)
        else:
            message = f"unknown expression: {kind}"
            raise SyntaxError(message)

    def generate_function(self, function: tuple, /) -> None:
        """Generate assembly for a single function definition."""
        _, name, parameters, body = function
        self.array_sizes = {}
        self.frame_size = 0
        self.locals = {}

        # Allocate parameters as locals.
        for _type, pname, _is_array in parameters:
            self.allocate_local(pname)

        self.scan_locals(body)

        self.emit(f"{name}:")
        if self.frame_size > 0:
            self.emit("        push bp")
            self.emit("        mov bp, sp")
            self.emit(f"        sub sp, {self.frame_size}")

        # Emit argc/argv startup for main with parameters.
        if name == "main" and parameters:
            self.emit_argument_vector_startup(parameters)

        for body_statement in body:
            self.generate_statement(body_statement)

        if self.frame_size > 0:
            self.emit("        mov sp, bp")
            self.emit("        pop bp")
        if name == "main":
            self.emit("        mov ah, SYS_EXIT")
            self.emit("        int 30h")
        else:
            self.emit("        ret")
        self.emit()

    def generate_if(self, statement: tuple, /) -> None:
        """Generate assembly for an if statement.

        Raises:
            SyntaxError: If the condition is not a comparison.

        """
        _, condition, body = statement
        label_index = self.new_label()
        if condition[0] != "binary_operator" or condition[1] not in JUMP_WHEN_FALSE:
            message = f"if condition must be a comparison, got {condition}"
            raise SyntaxError(message)
        _, operator, left, right = condition
        self.generate_expression(left)
        self.emit("        push ax")
        self.generate_expression(right)
        self.emit("        pop cx")
        self.emit("        cmp cx, ax")
        self.emit(f"        {JUMP_WHEN_FALSE[operator]} .if_{label_index}_end")
        for body_statement in body:
            self.generate_statement(body_statement)
        self.emit(f".if_{label_index}_end:")

    def generate_statement(self, statement: tuple, /) -> None:
        """Generate assembly for a single statement.

        Raises:
            SyntaxError: If an unknown statement kind is encountered.

        """
        kind = statement[0]
        if kind == "variable_declaration":
            _, vname, init = statement
            if init is not None:
                self.generate_expression(init)
                self.emit(f"        mov [bp-{self.locals[vname]}], ax")
        elif kind == "array_declaration":
            _, vname, init = statement
            if init is not None and init[0] == "array_init":
                elem_labels = []
                for elem in init[1]:
                    if elem[0] == "string":
                        label = f"_str_{len(self.strings)}"
                        self.strings.append((label, elem[1]))
                        elem_labels.append(label)
                    else:
                        message = "array initializer elements must be strings"
                        raise SyntaxError(message)
                arr_label = f"_arr_{len(self.arrays)}"
                self.arrays.append((arr_label, elem_labels))
                self.array_sizes[vname] = len(elem_labels)
                self.emit(f"        mov word [bp-{self.locals[vname]}], {arr_label}")
        elif kind == "assignment":
            _, vname, expression = statement
            self.generate_expression(expression)
            self.emit(f"        mov [bp-{self.locals[vname]}], ax")
        elif kind == "if":
            self.generate_if(statement)
        elif kind == "while":
            self.generate_while(statement)
        elif kind == "call":
            self.generate_call(statement)
        else:
            message = f"unknown statement: {kind}"
            raise SyntaxError(message)

    def generate_while(self, statement: tuple, /) -> None:
        """Generate assembly for a while loop.

        Raises:
            SyntaxError: If the while condition is not a comparison.

        """
        _, condition, body = statement
        label_index = self.new_label()
        self.emit(f".while_{label_index}:")
        if condition[0] != "binary_operator" or condition[1] not in JUMP_WHEN_FALSE:
            message = f"while condition must be a comparison, got {condition}"
            raise SyntaxError(message)
        _, operator, left, right = condition
        self.generate_expression(left)
        self.emit("        push ax")
        self.generate_expression(right)
        self.emit("        pop cx")
        self.emit("        cmp cx, ax")
        self.emit(f"        {JUMP_WHEN_FALSE[operator]} .while_{label_index}_end")
        for body_statement in body:
            self.generate_statement(body_statement)
        self.emit(f"        jmp .while_{label_index}")
        self.emit(f".while_{label_index}_end:")

    def new_label(self) -> int:
        """Allocate and return a new unique label index.

        Returns:
            The allocated label index.

        """
        label_index = self.label_id
        self.label_id += 1
        return label_index

    def scan_locals(self, statements: list[tuple], /) -> None:
        """Recursively find all variable declarations to size the frame."""
        for statement in statements:
            if statement[0] in {"variable_declaration", "array_declaration"}:
                self.allocate_local(statement[1])
            elif statement[0] == "while":
                self.scan_locals(statement[2])


class Parser:
    """Recursive descent parser for the C subset grammar."""

    def __init__(self, tokens: list[tuple[str, str, int]], /) -> None:
        """Initialize the parser with a token list."""
        self.tokens = tokens
        self.position = 0

    def eat(self, kind: str | None = None) -> tuple[str, str, int]:
        """Consume and return the current token, optionally checking its kind.

        Returns:
            The consumed token as a (kind, text, line) triple.

        Raises:
            SyntaxError: If the token kind does not match the expected kind.

        """
        token = self.tokens[self.position]
        if kind is not None and token[0] != kind:
            message = f"line {token[2]}: expected {kind}, got {token[0]} ({token[1]!r})"
            raise SyntaxError(message)
        self.position += 1
        return token

    def parse_additive(self) -> tuple:
        """Parse an additive expression (addition and subtraction).

        Returns:
            An AST node for the additive expression.

        """
        node = self.parse_multiplicative()
        while self.peek()[0] in ADDITIVE_OPERATORS:
            operator_token = self.eat()
            right = self.parse_multiplicative()
            node = ("binary_operator", operator_token[1], node, right)
        return node

    def parse_array_init(self) -> tuple:
        """Parse a brace-enclosed array initializer.

        Returns:
            An AST node for the array initializer.

        """
        self.eat("LBRACE")
        elems = [self.parse_expression()]
        while self.peek()[0] == "COMMA":
            self.eat("COMMA")
            elems.append(self.parse_expression())
        self.eat("RBRACE")
        return ("array_init", elems)

    def parse_assignment(self) -> tuple:
        """Parse a simple assignment statement.

        Returns:
            An AST node for the assignment.

        """
        name = self.eat("IDENT")[1]
        self.eat("ASSIGN")
        expression = self.parse_expression()
        self.eat("SEMI")
        return ("assignment", name, expression)

    def parse_call_statement(self) -> tuple:
        """Parse a function call statement.

        Returns:
            An AST node for the call statement.

        """
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

    def parse_comparison(self) -> tuple:
        """Parse a comparison expression.

        Returns:
            An AST node for the comparison expression.

        """
        left = self.parse_additive()
        if self.peek()[0] in COMPARISON_OPERATORS:
            operator_token = self.eat()
            right = self.parse_additive()
            return ("binary_operator", operator_token[1], left, right)
        return left

    def parse_compound_assignment(self) -> tuple:
        """Parse a compound assignment (+=) statement.

        Returns:
            An AST node for the desugared assignment.

        """
        name = self.eat("IDENT")[1]
        self.eat("PLUS_ASSIGN")
        expression = self.parse_expression()
        self.eat("SEMI")
        # Desugar: i += expr  →  i = i + expr
        return ("assignment", name, ("binary_operator", "+", ("variable", name), expression))

    def parse_expression(self) -> tuple:
        """Parse an expression.

        Returns:
            An AST node for the expression.

        """
        return self.parse_comparison()

    def parse_function(self) -> tuple:
        """Parse a function declaration.

        Returns:
            An AST node for the function.

        """
        self.parse_type()
        name = self.eat("IDENT")[1]
        self.eat("LPAREN")
        parameters = self.parse_parameters()
        self.eat("RPAREN")
        self.eat("LBRACE")
        body: list[tuple] = []
        while self.peek()[0] != "RBRACE":
            body.append(self.parse_statement())
        self.eat("RBRACE")
        return ("function", name, parameters, body)

    def parse_if(self) -> tuple:
        """Parse an if statement.

        Returns:
            An AST node for the if statement.

        """
        self.eat("IF")
        self.eat("LPAREN")
        condition = self.parse_expression()
        self.eat("RPAREN")
        self.eat("LBRACE")
        body: list[tuple] = []
        while self.peek()[0] != "RBRACE":
            body.append(self.parse_statement())
        self.eat("RBRACE")
        return ("if", condition, body)

    def parse_multiplicative(self) -> tuple:
        """Parse a multiplicative expression (multiplication and division).

        Returns:
            An AST node for the multiplicative expression.

        """
        node = self.parse_primary()
        while self.peek()[0] in MULTIPLICATIVE_OPERATORS:
            operator_token = self.eat()
            right = self.parse_primary()
            node = ("binary_operator", operator_token[1], node, right)
        return node

    def parse_parameter(self) -> tuple:
        """Parse a single function parameter.

        Returns:
            A (type, name, is_array) triple.

        """
        type_string = self.parse_type()
        name = self.eat("IDENT")[1]
        is_array = False
        if self.peek()[0] == "LBRACKET":
            self.eat("LBRACKET")
            self.eat("RBRACKET")
            is_array = True
        return (type_string, name, is_array)

    def parse_parameters(self) -> list[tuple]:
        """Parse a function parameter list.

        Returns:
            A list of (type, name, is_array) triples.

        """
        if self.peek()[0] == "RPAREN":
            return []
        parameters = [self.parse_parameter()]
        while self.peek()[0] == "COMMA":
            self.eat("COMMA")
            parameters.append(self.parse_parameter())
        return parameters

    def parse_primary(self) -> tuple:
        """Parse a primary expression (literals, variables, indexing, parens).

        Returns:
            An AST node for the primary expression.

        Raises:
            SyntaxError: If an unexpected token is encountered.

        """
        token = self.peek()
        if token[0] == "SIZEOF":
            return self.parse_sizeof()
        if token[0] == "NUMBER":
            self.eat()
            return ("int", int(token[1], 0))
        if token[0] == "CHAR_LIT":
            self.eat()
            return ("int", decode_first_character(token[1][1:-1]))
        if token[0] == "STRING":
            self.eat()
            return ("string", token[1][1:-1])
        if token[0] == "IDENT":
            self.eat()
            if self.peek()[0] == "LBRACKET":
                self.eat("LBRACKET")
                index = self.parse_expression()
                self.eat("RBRACKET")
                return ("index", token[1], index)
            return ("variable", token[1])
        if token[0] == "LPAREN":
            self.eat()
            expression = self.parse_expression()
            self.eat("RPAREN")
            return expression
        message = f"line {token[2]}: expected expression, got {token[0]} ({token[1]!r})"
        raise SyntaxError(message)

    def parse_program(self) -> tuple:
        """Parse the entire program as a sequence of function declarations.

        Returns:
            An AST node for the program.

        """
        functions = []
        while self.peek()[0] != "EOF":
            functions.append(self.parse_function())
        return ("program", functions)

    def parse_sizeof(self) -> tuple:
        """Parse a sizeof expression.

        Returns:
            An AST node for sizeof(type) or sizeof(variable).

        """
        self.eat("SIZEOF")
        self.eat("LPAREN")
        # sizeof(type) or sizeof(variable)
        if self.peek()[0] in TYPE_TOKENS:
            type_string = self.parse_type()
            self.eat("RPAREN")
            return ("sizeof_type", type_string)
        name = self.eat("IDENT")[1]
        self.eat("RPAREN")
        return ("sizeof_variable", name)

    def parse_statement(self) -> tuple:
        """Parse a single statement.

        Returns:
            An AST node for the statement.

        Raises:
            SyntaxError: If an unexpected token is encountered.

        """
        token = self.peek()
        if token[0] in TYPE_TOKENS:
            return self.parse_variable_declaration()
        if token[0] == "IF":
            return self.parse_if()
        if token[0] == "WHILE":
            return self.parse_while()
        if token[0] == "IDENT":
            next_kind = self.peek(offset=1)[0]
            if next_kind == "ASSIGN":
                return self.parse_assignment()
            if next_kind == "PLUS_ASSIGN":
                return self.parse_compound_assignment()
            return self.parse_call_statement()
        message = f"line {token[2]}: expected statement, got {token[0]} ({token[1]!r})"
        raise SyntaxError(message)

    def parse_type(self) -> str:
        """Parse a type specifier (void, int, char, char*).

        Returns:
            The type as a string.

        Raises:
            SyntaxError: If an unexpected token is encountered.

        """
        token = self.peek()
        if token[0] == "VOID":
            self.eat()
            return "void"
        if token[0] == "INT":
            self.eat()
            return "int"
        if token[0] == "CHAR":
            self.eat()
            if self.peek()[0] == "STAR":
                self.eat()
                return "char*"
            return "char"
        message = f"line {token[2]}: expected type, got {token[0]} ({token[1]!r})"
        raise SyntaxError(message)

    def parse_variable_declaration(self) -> tuple:
        """Parse a variable or array declaration.

        Returns:
            An AST node for the declaration.

        """
        self.parse_type()
        name = self.eat("IDENT")[1]
        # Optional [] for array declarations
        is_array = False
        if self.peek()[0] == "LBRACKET":
            self.eat("LBRACKET")
            self.eat("RBRACKET")
            is_array = True
        init = None
        if self.peek()[0] == "ASSIGN":
            self.eat("ASSIGN")
            init = self.parse_array_init() if is_array else self.parse_expression()
        self.eat("SEMI")
        if is_array:
            return ("array_declaration", name, init)
        return ("variable_declaration", name, init)

    def parse_while(self) -> tuple:
        """Parse a while loop statement.

        Returns:
            An AST node for the while loop.

        """
        self.eat("WHILE")
        self.eat("LPAREN")
        condition = self.parse_expression()
        self.eat("RPAREN")
        self.eat("LBRACE")
        body: list[tuple] = []
        while self.peek()[0] != "RBRACE":
            body.append(self.parse_statement())
        self.eat("RBRACE")
        return ("while", condition, body)

    def peek(self, offset: int = 0) -> tuple[str, str, int]:
        """Return the token at the current position plus an optional offset.

        Returns:
            The token as a (kind, text, line) triple.

        """
        return self.tokens[self.position + offset]


def decode_first_character(text: str) -> int:
    """Return the byte value of the first character in a C string literal.

    Returns:
        The integer byte value of the decoded character.

    """
    if text[0] == "\\" and len(text) >= 2:
        return CHARACTER_ESCAPES.get(text[1], ord(text[1]))
    return ord(text[0])


def main() -> int:
    """Compile a C source file to NASM assembly.

    Returns:
        Exit code (0 for success, 1 for usage error).

    """
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


def tokenize(source: str, /) -> list[tuple[str, str, int]]:
    """Tokenize C source code into a list of (kind, text, line) triples.

    Returns:
        A list of (kind, text, line) token triples.

    Raises:
        SyntaxError: If an unexpected character is encountered.

    """
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
        if kind in {"BLOCK_COMMENT", "LINE_COMMENT", "WS"}:
            line += text.count("\n")
        else:
            if kind == "IDENT" and text in KEYWORDS:
                kind = text.upper()
            tokens.append((kind, text, line))
        position = match.end()
    tokens.append(("EOF", "", line))
    return tokens


if __name__ == "__main__":
    sys.exit(main())
