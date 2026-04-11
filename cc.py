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
    multiplicative_expression := primary (('*'|'/'|'%') primary)*
    primary              := NUMBER | STRING | sizeof
                          | IDENT ('(' arguments ')' | '[' expression ']')?
                          | '(' expression ')'

Builtins:
    datetime(array)       -- fill 7-word array with BCD date/time fields
    die(message)          -- print message and terminate
    exit()                -- terminate program
    mkdir(name)           -- create directory, return 0 or ERR_* code
    print_bcd(expression) -- print BCD byte as two decimal digits
    print_dec(expression) -- print integer as decimal
    putc(expression)      -- print single character
    puts(expression)      -- print string (no auto-newline)
    uptime()              -- return seconds since boot

Usage: cc.py <input.c> [output.asm]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import ClassVar

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

JUMP_INVERT = {
    "ja": "jbe",
    "jae": "jb",
    "jb": "jae",
    "jbe": "ja",
    "je": "jne",
    "jg": "jle",
    "jge": "jl",
    "jl": "jge",
    "jle": "jg",
    "jne": "je",
}

JUMP_WHEN_FALSE = {
    "!=": "je",
    "<": "jge",
    "<=": "jg",
    ">": "jle",
    ">=": "jl",
    "==": "jne",
}

KEYWORDS = frozenset({"char", "else", "if", "int", "return", "sizeof", "void", "while"})

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

MULTIPLICATIVE_OPERATORS = frozenset({"PERCENT", "SLASH", "STAR"})

REGISTER_PARENT = {
    "ah": "ax",
    "al": "ax",
    "bh": "bx",
    "bl": "bx",
    "ch": "cx",
    "cl": "cx",
    "dh": "dx",
    "dl": "dx",
}

TYPE_TOKENS = frozenset({"CHAR", "INT", "VOID"})


class CodeGenerator:
    """Generates NASM x86 assembly from the parsed AST."""

    BUILTIN_CLOBBERS: ClassVar[dict[str, frozenset[str]]] = {
        "datetime": frozenset({"ax", "bx", "cx", "dx"}),
        "die": frozenset(),
        "exit": frozenset(),
        "mkdir": frozenset({"ax"}),
        "print_bcd": frozenset({"ax"}),
        "print_dec": frozenset({"ax", "bx", "cx", "dx"}),
        "putc": frozenset({"ax"}),
        "puts": frozenset({"ax"}),
        "uptime": frozenset({"ax"}),
    }

    ERROR_RETURNING_BUILTINS: ClassVar[frozenset[str]] = frozenset({"mkdir"})

    TYPE_SIZES: ClassVar[dict[str, int]] = {
        "char": 1,
        "char*": 2,
        "int": 2,
        "void": 0,
    }

    def __init__(self) -> None:
        """Initialize code generator state."""
        self.array_labels: dict[str, str] = {}
        self.array_sizes: dict[str, int] = {}
        self.arrays: list[tuple[str, list[str]]] = []
        self.division_remainder: tuple | None = None
        self.elide_frame: bool = False
        self.frame_size: int = 0
        self.label_id: int = 0
        self.lines: list[str] = []
        self.locals: dict[str, int] = {}
        self.ax_is_byte: bool = False
        self.ax_local: str | None = None
        self.die_count: int = 0
        self.needs_argv_buf: bool = False
        self.needs_print_bcd: bool = False
        self.needs_print_dec: bool = False
        self.register_cache: dict[tuple[str, int], str] = {}
        self.spill_stack: list[tuple[str, int]] = []
        self.strings: list[tuple[str, str]] = []

    def allocate_local(self, name: str, /) -> int:
        """Allocate a 2-byte local variable on the stack frame.

        Returns:
            The current frame size after allocation.

        """
        self.frame_size += 2
        self.locals[name] = self.frame_size
        return self.frame_size

    @staticmethod
    def always_exits(body: list[tuple], /) -> bool:
        """Check if a statement list always exits (die/exit/return)."""
        if not body:
            return False
        last = body[-1]
        if last[0] == "call" and last[1] in {"die", "exit"}:
            return True
        # Exhaustive if-else: both branches always exit.
        if last[0] == "if" and last[3] is not None:
            return CodeGenerator.always_exits(last[2]) and CodeGenerator.always_exits(last[3])
        return False

    def auto_spill(self, *, clobbers: frozenset[str]) -> None:
        """Spill cached registers that overlap with the clobber set.

        Pushes values onto the stack. Spills AX-parented entries first
        so AX is available as scratch for spilling other registers.
        """
        to_spill = [(key, register) for key, register in self.register_cache.items() if REGISTER_PARENT[register] in clobbers]
        if not to_spill:
            return
        to_spill.sort(key=lambda entry: REGISTER_PARENT[entry[1]] != "ax")
        for key, register in to_spill:
            if register != "al":
                self.emit(f"        mov al, {register}")
            self.emit("        push ax")
            self.spill_stack.append(key)
            del self.register_cache[key]

    def ax_clear(self) -> None:
        """Clear AX tracking state."""
        self.ax_is_byte = False
        self.ax_local = None

    def builtin_datetime(self, arguments: list[tuple], /) -> None:
        """Generate code for the datetime() builtin.

        Takes a pointer to a 7-word array and fills it with BCD fields:
        [0]=century, [1]=year, [2]=month, [3]=day,
        [4]=hours, [5]=minutes, [6]=seconds.

        When the argument is a variable backed by a known array label,
        fields are cached in registers and spilled lazily.
        """
        self.check_argument_count(arguments=arguments, expected=1, name="datetime")
        registers = ["ch", "cl", "dh", "dl", "bh", "bl", "al"]
        argument = arguments[0]
        array_label = self.array_labels.get(argument[1]) if argument[0] == "variable" else None
        if array_label:
            self.emit("        mov ah, SYS_RTC_DATETIME")
            self.emit("        int 30h")
            for index, register in enumerate(registers):
                self.register_cache[array_label, index * 2] = register
        else:
            self.generate_expression(argument)
            self.emit("        mov di, ax")
            self.emit("        mov ah, SYS_RTC_DATETIME")
            self.emit("        int 30h")
            self.emit("        mov [di+12], al")
            self.emit("        xor ah, ah")
            for index, register in enumerate(registers[:6]):
                self.emit(f"        mov al, {register}")
                self.emit(f"        mov [di+{index * 2}], ax")
            self.emit("        mov al, [di+12]")
            self.emit("        mov [di+12], ax")

    def builtin_die(self, arguments: list[tuple], /) -> None:
        """Generate code for the die() builtin.

        Prints a message and terminates via a shared label.
        """
        self.check_argument_count(arguments=arguments, expected=1, name="die")
        self.emit_si_from_argument(arguments[0])
        self.emit("        jmp .die")
        self.die_count += 1

    def builtin_exit(self, arguments: list[tuple], /) -> None:
        """Generate code for the exit() builtin."""
        self.check_argument_count(arguments=arguments, expected=0, name="exit")
        self.emit("        jmp .exit")

    def builtin_mkdir(self, arguments: list[tuple], /, *, fuse_exit: bool = False) -> None:
        """Generate code for the mkdir() builtin.

        Returns 0 on success or an ERR_* code on failure.  When
        *fuse_exit* is True, emits ``jnc .exit`` instead of converting
        the carry flag to a 0-or-error integer.
        """
        self.check_argument_count(arguments=arguments, expected=1, name="mkdir")
        self.emit_si_from_argument(arguments[0])
        self.emit("        mov ah, SYS_FS_MKDIR")
        self.emit("        int 30h")
        if fuse_exit:
            self.emit("        jnc .exit")
        else:
            label_index = self.new_label()
            self.emit(f"        jnc .ok_{label_index}")
            self.emit("        xor ah, ah")
            self.emit(f"        jmp .done_{label_index}")
            self.emit(f".ok_{label_index}:")
            self.emit("        xor ax, ax")
            self.emit(f".done_{label_index}:")

    def builtin_print_bcd(self, arguments: list[tuple], /) -> None:
        """Generate code for the print_bcd() builtin."""
        self.check_argument_count(arguments=arguments, expected=1, name="print_bcd")
        argument = arguments[0]
        cache_key = self.index_cache_key(argument)
        if cache_key and cache_key in self.register_cache:
            register = self.register_cache[cache_key]
            if register != "al":
                self.emit(f"        mov al, {register}")
        elif cache_key and self.spill_stack and self.spill_stack[-1] == cache_key:
            self.spill_stack.pop()
            self.emit("        pop ax")
        else:
            self.generate_expression(argument)
        self.emit("        call print_bcd")
        self.needs_print_bcd = True

    def builtin_print_dec(self, arguments: list[tuple], /) -> None:
        """Generate code for the print_dec() builtin."""
        self.check_argument_count(arguments=arguments, expected=1, name="print_dec")
        self.generate_expression(arguments[0])
        self.emit("        call print_dec")
        self.needs_print_dec = True

    def builtin_putc(self, arguments: list[tuple], /) -> None:
        """Generate code for the putc() builtin."""
        self.check_argument_count(arguments=arguments, expected=1, name="putc")
        argument = arguments[0]
        if argument[0] == "string":
            byte_val = decode_first_character(argument[1])
            self.emit(f"        mov al, {byte_val}")
        elif argument[0] == "int":
            self.emit(f"        mov al, {argument[1]}")
        else:
            self.generate_expression(argument)
        self.emit("        mov ah, SYS_IO_PUTC")
        self.emit("        int 30h")

    def builtin_puts(self, arguments: list[tuple], /) -> None:
        """Generate code for the puts() builtin."""
        self.check_argument_count(arguments=arguments, expected=1, name="puts")
        self.emit_si_from_argument(arguments[0])
        self.emit("        mov ah, SYS_IO_PUTS")
        self.emit("        int 30h")

    def builtin_uptime(self, arguments: list[tuple], /) -> None:
        """Generate code for the uptime() builtin."""
        self.check_argument_count(arguments=arguments, expected=0, name="uptime")
        self.emit("        mov ah, SYS_RTC_UPTIME")
        self.emit("        int 30h")

    @staticmethod
    def check_argument_count(*, arguments: list[tuple], expected: int, name: str) -> None:
        """Raise SyntaxError if the argument count doesn't match expected."""
        if expected == 0 and arguments:
            message = f"{name}() takes no arguments"
            raise SyntaxError(message)
        if expected > 0 and len(arguments) != expected:
            message = f"{name}() expects exactly {expected} argument{'s' if expected != 1 else ''}"
            raise SyntaxError(message)

    def check_defined(self, name: str, /) -> None:
        """Raise SyntaxError if a variable is not defined."""
        if name not in self.locals:
            message = f"undefined variable: {name}"
            raise SyntaxError(message)

    def emit(self, line: str = "") -> None:
        """Append a line of assembly to the output buffer."""
        self.lines.append(line)

    def emit_argument_vector_startup(self, parameters: list[tuple], /) -> None:
        """Emit inline startup code that parses EXEC_ARG into argc/argv."""
        # Single char* parameter: just load EXEC_ARG directly.
        if len(parameters) == 1 and parameters[0][0] == "char*" and not parameters[0][2]:
            pname = parameters[0][1]
            self.emit("        cld")
            self.emit("        mov ax, [EXEC_ARG]")
            self.emit(f"        mov [{self.local_address(pname)}], ax")
            self.ax_is_byte = False
            self.ax_local = pname
            return

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
            self.emit(f"        mov [{self.local_address(argc_name)}], cx")
        self.emit(f"        mov word [{self.local_address(argv_name)}], _argv")

    def emit_binary_operator_operands(self, left: tuple, right: tuple, /) -> None:
        """Generate left into AX and right into CX.

        When the right operand is a constant or variable, loads it
        directly into CX without a push/pop round-trip.
        """
        if right[0] == "int":
            self.generate_expression(left)
            self.emit(f"        mov cx, {right[1]}")
        elif right[0] == "variable" and right[1] in self.locals:
            self.generate_expression(left)
            self.emit(f"        mov cx, [{self.local_address(right[1])}]")
        else:
            self.generate_expression(left)
            self.emit("        push ax")
            self.generate_expression(right)
            self.emit("        mov cx, ax")
            self.emit("        pop ax")

    def emit_comparison(self, left: tuple, right: tuple, /) -> None:
        """Generate a comparison, leaving flags set for a conditional jump.

        Optimizes comparisons against integer constants by using
        ``cmp ax, imm`` directly, and ``test ax, ax`` for zero.
        """
        if right[0] == "int":
            self.generate_expression(left)
            if right[1] == 0:
                self.emit("        test al, al" if self.ax_is_byte else "        test ax, ax")
            else:
                register = "al" if self.ax_is_byte else "ax"
                self.emit(f"        cmp {register}, {right[1]}")
        else:
            self.emit_binary_operator_operands(left, right)
            self.emit("        cmp ax, cx")

    def emit_condition(self, *, condition: tuple, context: str) -> str:
        """Validate a condition, emit a comparison, and return the operator.

        Raises:
            SyntaxError: If the condition is not a comparison.

        """
        if condition[0] != "binary_operator" or condition[1] not in JUMP_WHEN_FALSE:
            message = f"{context} condition must be a comparison, got {condition}"
            raise SyntaxError(message)
        _, operator, left, right = condition
        self.emit_comparison(left, right)
        return operator

    def emit_si_from_argument(self, argument: tuple, /) -> None:
        """Load a string or expression argument into SI."""
        if argument[0] == "string":
            self.emit(f"        mov si, {self.new_string_label(argument[1])}")
        else:
            self.generate_expression(argument)
            self.emit("        mov si, ax")

    def emit_store_local(self, *, expression: tuple, name: str) -> None:
        """Generate an expression and store the result in a local variable."""
        self.generate_expression(expression)
        self.emit(f"        mov [{self.local_address(name)}], ax")
        self.ax_is_byte = False
        self.ax_local = name

    @staticmethod
    def extract_local_label(line: str, /) -> str | None:
        """Return the _l_ label from a store or declaration, or None."""
        # Store: mov [_l_NAME], ... or mov word [_l_NAME], ...
        if line.startswith("mov") and "[_l_" in line and "], " in line:
            return line[line.index("[_l_") + 1 : line.index("]")]
        # Declaration: _l_NAME: dw 0
        if line.startswith("_l_") and line.endswith(": dw 0"):
            return line[: line.index(":")]
        return None

    def fuse_trailing_puts(self, body: list[tuple], /) -> list[tuple]:
        """Transform trailing puts() calls into die() for main.

        Handles both a direct trailing ``puts(msg)`` and ``puts(msg)``
        at the end of branches in a trailing if-else chain.
        """
        if not body:
            return body
        last = body[-1]
        if last[0] == "call" and last[1] == "puts":
            return [*body[:-1], ("call", "die", last[2])]
        if last[0] == "if":
            transformed = self.transform_if_puts(last)
            if transformed is not last:
                return [*body[:-1], transformed]
        return body

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
        self.peephole()
        if self.strings:
            self.emit(";; --- string literals ---")
            for label, content in self.strings:
                self.emit(f"{label}: db `{content}\\0`")
        if self.arrays:
            code = "\n".join(self.lines)
            live = [(label, elements) for label, elements in self.arrays if label in code]
            if live:
                self.emit(";; --- array data ---")
                for label, elements in live:
                    self.emit(f"{label}: dw {', '.join(elements)}")
        if self.needs_argv_buf:
            self.emit("_argv: times 32 db 0")
        if self.needs_print_bcd:
            self.emit('%include "print_bcd.asm"')
        if self.needs_print_dec:
            self.emit('%include "print_dec.asm"')
        return "\n".join(self.lines) + "\n"

    def generate_body(self, statements: list[tuple], /) -> None:
        """Generate code for a sequence of statements.

        Applies two fusions:
        - ``puts(msg); exit();`` → ``die(msg)``
        - ``int err = syscall(...); if (err == 0) { exit(); }`` →
          syscall with ``jnc .exit`` (skip error-code conversion)
        """
        i = 0
        while i < len(statements):
            statement = statements[i]
            # Fuse puts() + exit() into die().
            if statement[0] == "call" and statement[1] == "puts" and i + 1 < len(statements) and statements[i + 1] == ("call", "exit", []):
                self.builtin_die(statement[2])
                i += 2
                continue
            # Fuse error-returning syscall + if-zero-exit.
            init = statement[2] if statement[0] == "variable_declaration" else None
            if init is not None and init[0] == "call" and init[1] in self.ERROR_RETURNING_BUILTINS and i + 1 < len(statements):
                next_stmt = statements[i + 1]
                skip = 0
                if self.is_zero_exit_if(next_stmt):
                    skip = 2  # skip declaration + if-zero-exit
                elif next_stmt[0] == "if" and not self.is_zero_test(next_stmt[1]):
                    skip = 1  # skip declaration only, process if-else normally
                if skip:
                    call_node = statement[2]
                    handler = getattr(self, f"builtin_{call_node[1]}")
                    clobbers = self.BUILTIN_CLOBBERS.get(call_node[1])
                    if self.register_cache and clobbers:
                        self.auto_spill(clobbers=clobbers)
                    handler(call_node[2], fuse_exit=True)
                    self.ax_is_byte = True
                    self.ax_local = statement[1]
                    i += skip
                    continue
            self.generate_statement(statement)
            i += 1

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
        clobbers = self.BUILTIN_CLOBBERS.get(name)
        if self.register_cache and clobbers:
            self.auto_spill(clobbers=clobbers)
        handler(arguments)

    def generate_expression(self, expression: tuple, /) -> None:
        """Generate code for an expression, leaving the result in AX.

        Raises:
            SyntaxError: If an unknown expression kind or operator is encountered.

        """
        kind = expression[0]
        # Skip load if AX already holds this variable's value.
        if kind == "variable" and expression[1] == self.ax_local:
            return
        if kind == "int":
            self.ax_clear()
            self.emit(f"        mov ax, {expression[1]}")
        elif kind == "string":
            self.ax_clear()
            self.emit(f"        mov ax, {self.new_string_label(expression[1])}")
        elif kind == "variable":
            vname = expression[1]
            self.check_defined(vname)
            self.emit(f"        mov ax, [{self.local_address(vname)}]")
            self.ax_is_byte = False
            self.ax_local = vname
        elif kind == "index":
            self.ax_clear()
            _, vname, index_expression = expression
            self.check_defined(vname)
            if index_expression[0] == "int" and vname in self.array_labels:
                offset = index_expression[1] * 2
                label = self.array_labels[vname]
                cache_key = (label, offset)
                if cache_key in self.register_cache:
                    register = self.register_cache[cache_key]
                    if register != "al":
                        self.emit(f"        mov al, {register}")
                    self.emit("        xor ah, ah")
                elif self.spill_stack and self.spill_stack[-1] == cache_key:
                    self.spill_stack.pop()
                    self.emit("        pop ax")
                elif offset:
                    self.emit(f"        mov ax, [{label}+{offset}]")
                else:
                    self.emit(f"        mov ax, [{label}]")
            elif index_expression[0] == "int":
                offset = index_expression[1] * 2
                self.emit(f"        mov bx, [{self.local_address(vname)}]")
                if offset:
                    self.emit(f"        mov ax, [bx+{offset}]")
                else:
                    self.emit("        mov ax, [bx]")
            else:
                self.emit("        push bx")
                self.generate_expression(index_expression)
                self.emit("        add ax, ax")
                self.emit("        pop bx")
                self.emit("        add bx, ax")
                self.emit("        mov ax, [bx]")
        elif kind == "sizeof_type":
            self.ax_clear()
            self.emit(f"        mov ax, {self.TYPE_SIZES.get(expression[1], 2)}")
        elif kind == "sizeof_variable":
            self.ax_clear()
            vname = expression[1]
            if vname in self.array_sizes:
                size = self.array_sizes[vname] * 2  # word-sized elements
            else:
                size = 2  # all non-array variables are word-sized
            self.emit(f"        mov ax, {size}")
        elif kind == "call":
            self.generate_call(expression)
        elif kind == "binary_operator":
            self.ax_clear()
            _, operator, left, right = expression
            if operator == "%" and self.has_remainder(left, right):
                self.emit("        mov ax, dx")
                return
            self.emit_binary_operator_operands(left, right)  # AX = left, CX = right
            if operator == "+":
                self.emit("        add ax, cx")
            elif operator == "-":
                self.emit("        sub ax, cx")
            elif operator == "*":
                self.emit("        mul cx")
                self.division_remainder = None
            elif operator in {"/", "%"}:
                self.emit("        xor dx, dx")
                self.emit("        div cx")
                self.division_remainder = (left, right)
                if operator == "%":
                    self.emit("        mov ax, dx")
            elif operator in JUMP_WHEN_FALSE:
                self.emit("        cmp ax, cx")
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
        self.array_labels = {}
        self.array_sizes = {}
        self.ax_clear()
        self.die_count = 0
        self.elide_frame = name == "main"
        self.frame_size = 0
        self.locals = {}
        self.register_cache = {}
        self.spill_stack = []

        # Allocate parameters as locals.
        for _type, pname, _is_array in parameters:
            self.allocate_local(pname)

        self.scan_locals(body)

        self.emit(f"{name}:")
        if not self.elide_frame and self.frame_size > 0:
            self.emit("        push bp")
            self.emit("        mov bp, sp")
            self.emit(f"        sub sp, {self.frame_size}")

        # Emit argc/argv startup for main with parameters.
        if name == "main" and parameters:
            self.emit_argument_vector_startup(parameters)

        # Fuse trailing puts() calls into die() since main exits implicitly.
        if name == "main":
            body = self.fuse_trailing_puts(body)
        self.generate_body(body)

        if name == "main":
            if self.die_count == 1:
                self.inline_single_die()
            elif self.die_count >= 2:
                self.emit("        jmp .exit")
                self.emit(".die:")
                self.emit("        mov ah, SYS_IO_PUTS")
                self.emit("        int 30h")
            self.emit(".exit:")
            self.emit("        mov ah, SYS_EXIT")
            self.emit("        int 30h")
            if self.elide_frame:
                for vname in sorted(self.locals):
                    self.emit(f"_l_{vname}: dw 0")
        else:
            if self.frame_size > 0:
                self.emit("        mov sp, bp")
                self.emit("        pop bp")
            self.emit("        ret")
        self.emit()

    def generate_if(self, statement: tuple, /) -> None:
        """Generate assembly for an if statement."""
        _, condition, body, else_body = statement
        label_index = self.new_label()
        operator = self.emit_condition(condition=condition, context="if")
        saved_ax = (self.ax_local, self.ax_is_byte)
        if else_body is not None:
            self.emit(f"        {JUMP_WHEN_FALSE[operator]} .if_{label_index}_else")
            self.generate_body(body)
            if_exits = self.always_exits(body)
            if not if_exits:
                self.emit(f"        jmp .if_{label_index}_end")
            self.emit(f".if_{label_index}_else:")
            # On the else path, AX is unchanged (comparison doesn't modify it).
            self.ax_local, self.ax_is_byte = saved_ax
            self.generate_body(else_body)
            if not if_exits or not self.always_exits(else_body):
                self.emit(f".if_{label_index}_end:")
            self.ax_clear()
        else:
            self.emit(f"        {JUMP_WHEN_FALSE[operator]} .if_{label_index}_end")
            self.generate_body(body)
            self.emit(f".if_{label_index}_end:")
            # If the body always exits, AX is unchanged on the fall-through path.
            if body and body[-1][0] == "call" and body[-1][1] in {"die", "exit"}:
                self.ax_local, self.ax_is_byte = saved_ax
            else:
                self.ax_clear()

    def generate_statement(self, statement: tuple, /) -> None:
        """Generate assembly for a single statement.

        Raises:
            SyntaxError: If an unknown statement kind is encountered.

        """
        kind = statement[0]
        if kind == "variable_declaration":
            _, vname, init = statement
            if init is not None:
                self.emit_store_local(expression=init, name=vname)
        elif kind == "array_declaration":
            _, vname, init = statement
            if init is not None and init[0] == "array_init":
                elem_labels = []
                for elem in init[1]:
                    if elem[0] == "string":
                        elem_labels.append(self.new_string_label(elem[1]))
                    elif elem[0] == "int":
                        elem_labels.append(str(elem[1]))
                    else:
                        message = "array initializer elements must be constants"
                        raise SyntaxError(message)
                array_label = f"_arr_{len(self.arrays)}"
                self.arrays.append((array_label, elem_labels))
                self.array_labels[vname] = array_label
                self.array_sizes[vname] = len(elem_labels)
                self.emit(f"        mov word [{self.local_address(vname)}], {array_label}")
        elif kind == "assignment":
            self.emit_store_local(expression=statement[2], name=statement[1])
        elif kind == "if":
            self.generate_if(statement)
        elif kind == "while":
            self.ax_clear()
            self.generate_while(statement)
        elif kind == "call":
            self.generate_call(statement)
            self.ax_clear()
        else:
            message = f"unknown statement: {kind}"
            raise SyntaxError(message)

    def generate_while(self, statement: tuple, /) -> None:
        """Generate assembly for a while loop."""
        _, condition, body = statement
        label_index = self.new_label()
        self.emit(f".while_{label_index}:")
        operator = self.emit_condition(condition=condition, context="while")
        self.emit(f"        {JUMP_WHEN_FALSE[operator]} .while_{label_index}_end")
        self.generate_body(body)
        self.emit(f"        jmp .while_{label_index}")
        self.emit(f".while_{label_index}_end:")

    def has_remainder(self, left: tuple, right: tuple, /) -> bool:
        """Check if DX already holds left % right.

        Handles both direct matches and the transitive property:
        (A % N) % M == A % M when M divides N.
        """
        if self.division_remainder is None:
            return False
        remainder_left, remainder_right = self.division_remainder
        # Direct match: same operands.
        if remainder_left == left and remainder_right == right:
            return True
        # Transitive: DX = (A % N) % M, want A % M, and M divides N.
        return (
            remainder_right == right
            and right[0] == "int"
            and self.is_modulo_of(base=left, expression=remainder_left)
            and remainder_left[3][1] % right[1] == 0
        )

    def index_cache_key(self, expression: tuple, /) -> tuple[str, int] | None:
        """Return the register cache key for an index expression, or None."""
        if expression[0] == "index" and expression[2][0] == "int" and expression[1] in self.array_labels:
            return (self.array_labels[expression[1]], expression[2][1] * 2)
        return None

    def inline_single_die(self) -> None:
        """Replace a lone ``jmp .die`` with inline puts + ``jmp .exit``."""
        for i, line in enumerate(self.lines):
            if line.strip() == "jmp .die":
                self.lines[i] = "        mov ah, SYS_IO_PUTS"
                self.lines.insert(i + 1, "        int 30h")
                self.lines.insert(i + 2, "        jmp .exit")
                return

    @staticmethod
    def is_modulo_of(*, base: tuple, expression: tuple) -> bool:
        """Check if expression is (base % N) for some integer N."""
        return (
            len(expression) == 4
            and expression[0] == "binary_operator"
            and expression[1] == "%"
            and expression[2] == base
            and expression[3][0] == "int"
        )

    @staticmethod
    def is_zero_exit_if(statement: tuple, /) -> bool:
        """Check if a statement is ``if (VAR == 0) { exit(); }``."""
        return (
            statement[0] == "if"
            and statement[1][0] == "binary_operator"
            and statement[1][1] == "=="
            and statement[1][3] == ("int", 0)
            and len(statement[2]) == 1
            and statement[2][0] == ("call", "exit", [])
            and statement[3] is None
        )

    @staticmethod
    def is_zero_test(condition: tuple, /) -> bool:
        """Check if a condition tests ``VAR == 0``."""
        return condition[0] == "binary_operator" and condition[1] == "==" and condition[3] == ("int", 0)

    def local_address(self, name: str, /) -> str:
        """Return the memory operand string for a local variable."""
        if self.elide_frame:
            return f"_l_{name}"
        return f"bp-{self.locals[name]}"

    def new_label(self) -> int:
        """Allocate and return a new unique label index.

        Returns:
            The allocated label index.

        """
        label_index = self.label_id
        self.label_id += 1
        return label_index

    def new_string_label(self, content: str, /) -> str:
        """Allocate a string literal and return its label name."""
        label = f"_str_{len(self.strings)}"
        self.strings.append((label, content))
        return label

    def peephole(self) -> None:
        """Run peephole optimization passes over generated assembly."""
        self.peephole_dead_code()
        self.peephole_double_jump()
        self.peephole_jump_next()
        self.peephole_store_reload()
        self.peephole_dead_stores()

    def peephole_dead_code(self) -> None:
        """Remove unreachable instructions after unconditional jumps."""
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("jmp ") and not b.endswith(":"):
                del self.lines[i + 1]
                continue
            i += 1

    def peephole_dead_stores(self) -> None:
        """Remove stores to local variables that are never loaded."""
        # Collect all _l_ labels that appear as load sources.
        loaded: set[str] = set()
        for line in self.lines:
            stripped = line.strip()
            index = stripped.find(", [_l_")
            if index >= 0:
                loaded.add(stripped[index + 3 : stripped.index("]", index)])
        # Remove stores and declarations for labels never loaded.
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            label = self.extract_local_label(stripped)
            if label is not None and label not in loaded:
                continue
            result.append(line)
        self.lines = result

    def peephole_double_jump(self) -> None:
        """Collapse conditional-jump-over-unconditional-jump sequences.

        Replaces ``jCC .L1 / jmp .L2 / .L1:`` with ``jCC_inv .L2``.
        """
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            # Match: jCC .label1 / jmp .label2 / .label1:
            parts = a.split()
            if len(parts) == 2 and parts[0] in JUMP_INVERT and b.startswith("jmp ") and c == f"{parts[1]}:":
                target = b.split()[1]
                self.lines[i] = f"        {JUMP_INVERT[parts[0]]} {target}"
                del self.lines[i + 1 : i + 3]
                continue
            i += 1

    def peephole_jump_next(self) -> None:
        """Remove unconditional jumps to the immediately following label."""
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("jmp ") and b == f"{a.split()[1]}:":
                del self.lines[i]
                continue
            i += 1

    def peephole_store_reload(self) -> None:
        """Remove redundant store-then-reload sequences."""
        i = 0
        while i < len(self.lines) - 1:
            line = self.lines[i].strip()
            next_line = self.lines[i + 1].strip()
            # mov [ADDR], ax  followed by  mov ax, [ADDR]  →  drop the reload
            if line.startswith("mov [") and line.endswith("], ax") and next_line == f"mov ax, {line[4 : line.index(']') + 1]}":
                del self.lines[i + 1]
                continue
            i += 1

    def scan_locals(self, statements: list[tuple], /) -> None:
        """Recursively find all variable declarations to size the frame."""
        for statement in statements:
            if statement[0] in {"variable_declaration", "array_declaration"}:
                self.allocate_local(statement[1])
            elif statement[0] == "if":
                self.scan_locals(statement[2])
                if statement[3] is not None:
                    self.scan_locals(statement[3])
            elif statement[0] == "while":
                self.scan_locals(statement[2])

    @staticmethod
    def transform_branch_puts(body: list[tuple], /) -> list[tuple]:
        """Replace trailing puts(msg) with die(msg) in a branch body."""
        if body and body[-1][0] == "call" and body[-1][1] == "puts":
            return [*body[:-1], ("call", "die", body[-1][2])]
        return body

    def transform_if_puts(self, statement: tuple, /) -> tuple:
        """Transform puts() at end of if-else branches into die()."""
        _, condition, if_body, else_body = statement
        new_if = self.transform_branch_puts(if_body)
        new_else = else_body
        if else_body is not None:
            if len(else_body) == 1 and else_body[0][0] == "if":
                transformed = self.transform_if_puts(else_body[0])
                if transformed is not else_body[0]:
                    new_else = [transformed]
            else:
                new_else = self.transform_branch_puts(else_body)
        if new_if is if_body and new_else is else_body:
            return statement
        return ("if", condition, new_if, new_else)


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

    def parse_arguments(self) -> list[tuple]:
        """Parse a comma-separated argument list through the closing paren.

        Returns:
            A list of AST expression nodes.

        """
        arguments: list[tuple] = []
        if self.peek()[0] != "RPAREN":
            arguments.append(self.parse_expression())
            while self.peek()[0] == "COMMA":
                self.eat("COMMA")
                arguments.append(self.parse_expression())
        self.eat("RPAREN")
        return arguments

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

    def parse_block(self) -> list[tuple]:
        """Parse statements until a closing brace and consume it.

        Returns:
            A list of AST statement nodes.

        """
        body: list[tuple] = []
        while self.peek()[0] != "RBRACE":
            body.append(self.parse_statement())
        self.eat("RBRACE")
        return body

    def parse_call_statement(self) -> tuple:
        """Parse a function call statement.

        Returns:
            An AST node for the call statement.

        """
        name = self.eat("IDENT")[1]
        self.eat("LPAREN")
        arguments = self.parse_arguments()
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
        return ("function", name, parameters, self.parse_block())

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
        body = self.parse_block()
        else_body: list[tuple] | None = None
        if self.peek()[0] == "ELSE":
            self.eat("ELSE")
            if self.peek()[0] == "IF":
                else_body = [self.parse_if()]
            else:
                self.eat("LBRACE")
                else_body = self.parse_block()
        return ("if", condition, body, else_body)

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
            if self.peek()[0] == "LPAREN":
                self.eat("LPAREN")
                return ("call", token[1], self.parse_arguments())
            if self.peek()[0] == "LBRACKET":
                self.eat("LBRACKET")
                index = self.parse_expression()
                self.eat("RBRACKET")
                return ("index", token[1], index)
            return ("variable", token[1])
        if token[0] == "NOT":
            self.eat()
            return ("binary_operator", "==", self.parse_primary(), ("int", 0))
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
        if token[0] == "RETURN":
            self.eat("RETURN")
            self.eat("SEMI")
            return ("call", "exit", [])
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
        return ("while", condition, self.parse_block())

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
