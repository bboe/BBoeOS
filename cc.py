#!/usr/bin/env python3
"""Minimal C subset compiler for BBoeOS.

Compiles a tiny subset of C to NASM-compatible assembly that the BBoeOS
self-hosted assembler (or host NASM) can assemble into a flat binary.

Grammar:
    program              := function_declaration*
    function_declaration := type IDENT '(' ')' '{' statement* '}'
    type                 := 'void' | 'int' | 'char' '*'
    statement            := variable_declaration | assign_statement | do_while_statement
                          | while_statement | call_statement
    variable_declaration := type IDENT ('[' ']')? '=' (expression
                          | '{' expression_list '}') ';'

Register allocation:
    Plain ``int`` locals in main are auto-pinned to a CPU register
    (DX, CX, BX, DI — in declaration order) unless the initializer is
    a call, in which case the value would interfere with error fusion
    and the variable goes into memory instead.
    assign               := IDENT '=' expression ';'
                          |  IDENT '+=' expression ';'
    do_while             := 'do' '{' statement* '}' 'while' '(' expression ')' ';'
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
    close(fd)                -- close a file descriptor
    datetime()               -- return unsigned seconds since 1970-01-01 UTC
    die(message)             -- print message and terminate
    exit()                   -- terminate program
    mkdir(name)              -- create directory, return 0 or ERR_* code
    open(name, flags)        -- open file, return fd or -1 on error
    print_datetime(epoch)    -- print epoch as YYYY-MM-DD HH:MM:SS
    putc(expression)         -- print single character
    read(fd, buffer, count)  -- read bytes from fd, return count or -1
    uptime()                 -- return seconds since boot
    write(fd, buffer, count) -- write bytes to fd, return count or -1

Usage: cc.py <input.c> [output.asm]
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


@dataclass(slots=True)
class Node:
    pass


@dataclass(slots=True)
class Param:
    type: str
    name: str
    is_array: bool


@dataclass(slots=True)
class ArrayDecl(Node):
    name: str
    type_name: str
    init: Node | None


@dataclass(slots=True)
class ArrayInit(Node):
    elements: list[Node]


@dataclass(slots=True)
class Assign(Node):
    name: str
    expr: Node


@dataclass(slots=True)
class BinOp(Node):
    op: str
    left: Node
    right: Node


@dataclass(slots=True)
class Break(Node):
    pass


@dataclass(slots=True)
class Call(Node):
    name: str
    args: list[Node]


@dataclass(slots=True)
class DoWhile(Node):
    cond: Node
    body: list[Node]


@dataclass(slots=True)
class Function(Node):
    name: str
    params: list[Param]
    body: list[Node]


@dataclass(slots=True)
class If(Node):
    cond: Node
    body: list[Node]
    else_body: list[Node] | None


@dataclass(slots=True)
class Index(Node):
    name: str
    index: Node


@dataclass(slots=True)
class Int(Node):
    value: int


class Char(Int):
    """A ``char`` literal (``'x'``).

    Subclasses :class:`Int` so existing ``isinstance(..., Int)`` checks
    continue to treat char literals as integer constants for codegen,
    while type-checking (e.g., equality operand validation) can still
    distinguish char literals from plain integers.

    Placed directly after :class:`Int` because the subclass relationship
    requires its base to be defined first.
    """

    __slots__ = ()


@dataclass(slots=True)
class Program(Node):
    functions: list[Node]


@dataclass(slots=True)
class SizeofType(Node):
    type_name: str


@dataclass(slots=True)
class SizeofVar(Node):
    name: str


@dataclass(slots=True)
class String(Node):
    content: str


@dataclass(slots=True)
class Var(Node):
    name: str


@dataclass(slots=True)
class VarDecl(Node):
    name: str
    type_name: str
    init: Node | None


@dataclass(slots=True)
class While(Node):
    cond: Node
    body: list[Node]

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

KEYWORDS = frozenset({"break", "char", "do", "else", "if", "int", "long", "return", "sizeof", "unsigned", "void", "while"})

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

TYPE_TOKENS = frozenset({"CHAR", "INT", "LONG", "UNSIGNED", "VOID"})


class CodeGenerator:
    """Generates NASM x86 assembly from the parsed AST."""

    BUILTIN_CLOBBERS: ClassVar[dict[str, frozenset[str]]] = {
        "chmod": frozenset({"ax", "si"}),
        "close": frozenset({"ax"}),
        "datetime": frozenset({"ax", "dx"}),
        "die": frozenset(),
        "exit": frozenset(),
        "fstat": frozenset({"ax", "bx", "cx", "dx"}),
        "mkdir": frozenset({"ax"}),
        "open": frozenset({"ax", "dx"}),
        "print_datetime": frozenset({"ax", "bx", "cx", "dx", "si"}),
        "printf": frozenset({"ax", "bx", "cx", "dx", "si", "di"}),
        "putc": frozenset({"ax"}),
        "read": frozenset({"ax", "cx", "di"}),
        "rename": frozenset({"ax", "di", "si"}),
        "strlen": frozenset({"ax", "cx", "di"}),
        "uptime": frozenset({"ax"}),
        "write": frozenset({"ax", "cx", "si"}),
    }

    ERROR_RETURNING_BUILTINS: ClassVar[frozenset[str]] = frozenset({"chmod", "mkdir", "rename"})

    #: Identifiers that resolve to NASM kernel constants rather than
    #: user-defined variables.  Emitted verbatim so NASM can resolve
    #: them from ``constants.asm``.
    NAMED_CONSTANTS: ClassVar[frozenset[str]] = frozenset({
        "ERROR_EXISTS",
        "ERROR_NOT_FOUND",
        "ERROR_PROTECTED",
        "FLAG_DIRECTORY",
        "FLAG_EXECUTE",
        "NULL",
        "O_CREAT",
        "O_RDONLY",
        "O_TRUNC",
        "O_WRONLY",
        "SECTOR_BUFFER",
        "STDERR",
        "STDIN",
        "STDOUT",
    })

    #: Registers available for auto-pinning, in allocation order.
    REGISTER_POOL: ClassVar[tuple[str, ...]] = ("dx", "cx", "bx", "di")

    TYPE_SIZES: ClassVar[dict[str, int]] = {
        "char": 1,
        "char*": 2,
        "int": 2,
        "unsigned long": 4,
        "void": 0,
    }

    def __init__(self) -> None:
        """Initialize code generator state."""
        self.array_labels: dict[str, str] = {}
        self.array_sizes: dict[str, int] = {}
        self.arrays: list[tuple[str, list[str]]] = []
        self.ax_is_byte: bool = False
        self.ax_local: str | None = None

        self.division_remainder: tuple | None = None
        self.elide_frame: bool = False
        self.frame_size: int = 0
        self.label_id: int = 0
        self.lines: list[str] = []
        self.live_long_local: str | None = None
        self.locals: dict[str, int] = {}
        self.loop_end_labels: list[str] = []
        self.needs_argv_buf: bool = False
        self.pinned_register: dict[str, str] = {}
        self.register_cache: dict[tuple[str, int], str] = {}
        self.spill_stack: list[tuple[str, int]] = []
        self.strings: list[tuple[str, str]] = []
        self.variable_arrays: set[str] = set()
        self.variable_types: dict[str, str] = {}
        self.virtual_long_locals: set[str] = set()
        self.visible_vars: set[str] = set()

    def allocate_local(self, name: str, /, *, size: int = 2) -> int:
        """Allocate a local variable on the stack frame.

        Args:
            name: local variable name.
            size: slot size in bytes (2 for ints/pointers, 4 for unsigned long).

        Returns:
            The current frame size after allocation.

        """
        self.frame_size += size
        self.locals[name] = self.frame_size
        return self.frame_size

    @staticmethod
    def always_exits(body: list[Node], /) -> bool:
        """Check if a statement list always exits its enclosing block.

        Recognizes ``die(...)``/``exit()``/``return`` (program exits)
        and ``break`` (loop exit).  Used to elide dead fall-through
        code and to keep AX tracking alive across an if whose body
        never falls through.
        """
        if not body:
            return False
        last = body[-1]
        if isinstance(last, Break):
            return True
        if isinstance(last, Call) and last.name in {"die", "exit"}:
            return True
        # Exhaustive if-else: both branches always exit.
        if isinstance(last, If) and last.else_body is not None:
            return CodeGenerator.always_exits(last.body) and CodeGenerator.always_exits(last.else_body)
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

    def builtin_chmod(self, arguments: list[Node], /, *, fuse_exit: bool = False) -> None:
        """Generate code for the chmod() builtin.

        Returns 0 on success or an ERR_* code on failure.  When
        *fuse_exit* is True, emits ``jnc FUNCTION_EXIT`` instead of
        converting the carry flag to a 0-or-error integer.
        """
        self.check_argument_count(arguments=arguments, expected=2, name="chmod")
        self.emit_si_from_argument(arguments[0])
        self.generate_expression(arguments[1])
        self.emit("        mov ah, SYS_FS_CHMOD")
        self.emit("        int 30h")
        if fuse_exit:
            self.emit("        jnc FUNCTION_EXIT")
        else:
            label_index = self.new_label()
            self.emit(f"        jnc .ok_{label_index}")
            self.emit("        xor ah, ah")
            self.emit(f"        jmp .done_{label_index}")
            self.emit(f".ok_{label_index}:")
            self.emit("        xor ax, ax")
            self.emit(f".done_{label_index}:")

    def builtin_close(self, arguments: list[Node], /) -> None:
        """Generate code for the close() builtin.

        Closes a file descriptor.  ``close(fd)`` emits
        ``mov bx, <fd> / mov ah, SYS_IO_CLOSE / int 30h``.
        """
        self.check_argument_count(arguments=arguments, expected=1, name="close")
        self.emit_register_from_argument(argument=arguments[0], register="bx")
        self.emit("        mov ah, SYS_IO_CLOSE")
        self.emit("        int 30h")

    def builtin_datetime(self, arguments: list[Node], /) -> None:
        """Generate code for the datetime() builtin.

        Returns unsigned seconds since 1970-01-01 UTC in DX:AX. Valid
        through the year 2106 (32-bit epoch overflow).
        """
        self.check_argument_count(arguments=arguments, expected=0, name="datetime")
        self.emit("        mov ah, SYS_RTC_DATETIME")
        self.emit("        int 30h")

    def builtin_die(self, arguments: list[Node], /) -> None:
        """Generate code for the die() builtin.

        Pre-loads SI and CX (string + length) and jumps to a shared
        ``.die`` label that calls ``write_stdout`` then exits.
        """
        self.check_argument_count(arguments=arguments, expected=1, name="die")
        argument = arguments[0]
        if not isinstance(argument, String):
            message = "die() requires a string literal"
            raise SyntaxError(message)
        label = self.new_string_label(argument.content)
        length = string_byte_length(argument.content)
        self.emit(f"        mov si, {label}")
        self.emit(f"        mov cx, {length}")
        self.emit("        jmp FUNCTION_DIE")

    def builtin_exit(self, arguments: list[Node], /) -> None:
        """Generate code for the exit() builtin."""
        self.check_argument_count(arguments=arguments, expected=0, name="exit")
        self.emit("        jmp FUNCTION_EXIT")

    def builtin_fstat(self, arguments: list[Node], /) -> None:
        """Generate code for the fstat() builtin.

        ``fstat(fd)`` emits ``mov bx, <fd> / mov ah, SYS_IO_FSTAT /
        int 30h``.  Returns the file mode (flags byte) in AX.
        The syscall also returns CX:DX = file size, but those are
        discarded here.
        """
        self.check_argument_count(arguments=arguments, expected=1, name="fstat")
        self.emit_register_from_argument(argument=arguments[0], register="bx")
        self.emit("        mov ah, SYS_IO_FSTAT")
        self.emit("        int 30h")
        self.emit("        xor ah, ah")

    def builtin_mkdir(self, arguments: list[Node], /, *, fuse_exit: bool = False) -> None:
        """Generate code for the mkdir() builtin.

        Returns 0 on success or an ERR_* code on failure.  When
        *fuse_exit* is True, emits ``jnc FUNCTION_EXIT`` instead of converting
        the carry flag to a 0-or-error integer.
        """
        self.check_argument_count(arguments=arguments, expected=1, name="mkdir")
        self.emit_si_from_argument(arguments[0])
        self.emit("        mov ah, SYS_FS_MKDIR")
        self.emit("        int 30h")
        if fuse_exit:
            self.emit("        jnc FUNCTION_EXIT")
        else:
            label_index = self.new_label()
            self.emit(f"        jnc .ok_{label_index}")
            self.emit("        xor ah, ah")
            self.emit(f"        jmp .done_{label_index}")
            self.emit(f".ok_{label_index}:")
            self.emit("        xor ax, ax")
            self.emit(f".done_{label_index}:")

    def builtin_open(self, arguments: list[Node], /) -> None:
        """Generate code for the open() builtin.

        ``open(name, flags)`` or ``open(name, flags, mode)`` emits
        ``mov si, <name> / mov al, <flags> / [mov dl, <mode>] /
        mov ah, SYS_IO_OPEN / int 30h``.  The optional *mode*
        parameter sets the file permission flags (e.g. ``FLAG_EXECUTE``)
        when ``O_CREAT`` creates a new file.  Returns the fd number
        in AX, or -1 on error (CF set).
        """
        if len(arguments) < 2 or len(arguments) > 3:
            message = "open() expects 2 or 3 arguments"
            raise SyntaxError(message)
        name_argument = arguments[0]
        flags_argument = arguments[1]
        self.emit_si_from_argument(name_argument)
        if isinstance(flags_argument, Int) or (isinstance(flags_argument, Var) and flags_argument.name in self.NAMED_CONSTANTS):
            self.emit(f"        mov al, {flags_argument.value if isinstance(flags_argument, Int) else flags_argument.name}")
        else:
            self.generate_expression(flags_argument)
        if len(arguments) == 3:
            self.emit_register_from_argument(argument=arguments[2], register="dl")
        self.emit("        mov ah, SYS_IO_OPEN")
        self.emit("        int 30h")
        self.ax_clear()

    def builtin_print_datetime(self, arguments: list[Node], /) -> None:
        """Generate code for the print_datetime(unsigned long) builtin.

        Prints the epoch value as ``YYYY-MM-DD HH:MM:SS`` (no newline).
        """
        self.check_argument_count(arguments=arguments, expected=1, name="print_datetime")
        self.generate_long_expression(arguments[0])
        self.emit("        call FUNCTION_PRINT_DATETIME")

    def builtin_printf(self, arguments: list[Node], /) -> None:
        """Generate code for the printf() builtin.

        First argument must be a string literal.  Remaining arguments
        are pushed right-to-left onto the stack, followed by the format
        string pointer.  Uses cdecl calling convention (caller cleans).

        Optimization: when the format string contains no ``%`` at all
        (no format specifiers, no ``%%`` escapes), emits a direct
        ``call FUNCTION_PRINT_STRING`` instead of the full printf
        machinery.
        """
        if not arguments or not isinstance(arguments[0], String):
            message = "printf() requires a string literal as the first argument"
            raise SyntaxError(message)
        fmt = arguments[0].content
        # Fast path: no '%' at all → emit print_string directly.
        if "%" not in fmt and len(arguments) == 1:
            label = self.new_string_label(fmt)
            self.emit(f"        mov di, {label}")
            self.emit("        call FUNCTION_PRINT_STRING")
            return
        # Count format specifiers (excluding %%) to validate argument count.
        expected_args = 0
        i = 0
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt):
                if fmt[i + 1] != "%":
                    expected_args += 1
                i += 2
            else:
                i += 1
        if len(arguments) - 1 != expected_args:
            message = f"printf() format expects {expected_args} argument{'s' if expected_args != 1 else ''}, got {len(arguments) - 1}"
            raise SyntaxError(message)
        # Push arguments right-to-left.
        for arg in reversed(arguments[1:]):
            self.generate_expression(arg)
            self.emit("        push ax")
        # Push format string pointer.
        label = self.new_string_label(fmt)
        self.emit(f"        push {label}")
        self.emit("        call FUNCTION_PRINTF")
        stack_size = len(arguments) * 2
        self.emit(f"        add sp, {stack_size}")

    def builtin_putc(self, arguments: list[Node], /) -> None:
        """Generate code for the putc() builtin."""
        self.check_argument_count(arguments=arguments, expected=1, name="putc")
        argument = arguments[0]
        if isinstance(argument, String):
            byte_val = decode_first_character(argument.content)
            self.emit(f"        mov al, {byte_val}")
        elif isinstance(argument, Int):
            self.emit(f"        mov al, {argument.value}")
        else:
            self.generate_expression(argument)
        self.emit("        call FUNCTION_PRINT_CHARACTER")

    def builtin_read(self, arguments: list[Node], /) -> None:
        """Generate code for the read() builtin.

        ``read(fd, buffer, count)`` emits ``mov bx, <fd> /
        mov di, <buffer> / mov cx, <count> / mov ah, SYS_IO_READ /
        int 30h``.  Returns bytes read in AX (0 = EOF, -1 = error).
        """
        self.check_argument_count(arguments=arguments, expected=3, name="read")
        fd_argument, buffer_argument, count_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self.emit_register_from_argument(argument=buffer_argument, register="di")
        self.emit_register_from_argument(argument=count_argument, register="cx")
        self.emit("        mov ah, SYS_IO_READ")
        self.emit("        int 30h")
        self.ax_clear()

    def builtin_rename(self, arguments: list[Node], /, *, fuse_exit: bool = False) -> None:
        """Generate code for the rename() builtin.

        ``rename(oldname, newname)`` emits ``mov si, <oldname> /
        mov di, <newname> / mov ah, SYS_FS_RENAME / int 30h``.
        Returns 0 on success or an ERROR_* code on failure.  When
        *fuse_exit* is True, emits ``jnc FUNCTION_EXIT`` instead of
        converting the carry flag to a 0-or-error integer.
        """
        self.check_argument_count(arguments=arguments, expected=2, name="rename")
        self.emit_si_from_argument(arguments[0])
        self.emit_register_from_argument(argument=arguments[1], register="di")
        self.emit("        mov ah, SYS_FS_RENAME")
        self.emit("        int 30h")
        if fuse_exit:
            self.emit("        jnc FUNCTION_EXIT")
        else:
            label_index = self.new_label()
            self.emit(f"        jnc .ok_{label_index}")
            self.emit("        xor ah, ah")
            self.emit(f"        jmp .done_{label_index}")
            self.emit(f".ok_{label_index}:")
            self.emit("        xor ax, ax")
            self.emit(f".done_{label_index}:")

    def builtin_strlen(self, arguments: list[Node], /) -> None:
        """Generate code for the strlen() builtin.

        ``strlen(ptr)`` scans for a null terminator and returns the
        string length in AX.  Uses ``repne scasb`` (clobbers CX, DI).
        """
        self.check_argument_count(arguments=arguments, expected=1, name="strlen")
        self.emit_register_from_argument(argument=arguments[0], register="di")
        self.emit("        xor al, al")
        self.emit("        mov cx, 0FFFFh")
        self.emit("        cld")
        self.emit("        repne scasb")
        self.emit("        mov ax, 0FFFEh")
        self.emit("        sub ax, cx")
        self.ax_clear()

    def builtin_uptime(self, arguments: list[Node], /) -> None:
        """Generate code for the uptime() builtin."""
        self.check_argument_count(arguments=arguments, expected=0, name="uptime")
        self.emit("        mov ah, SYS_RTC_UPTIME")
        self.emit("        int 30h")

    def builtin_write(self, arguments: list[Node], /) -> None:
        """Generate code for the write() builtin.

        ``write(fd, buffer, count)`` emits ``mov bx, <fd> /
        mov si, <buffer> / mov cx, <count> / mov ah, SYS_IO_WRITE /
        int 30h``.  Returns bytes written in AX (-1 on error).
        """
        self.check_argument_count(arguments=arguments, expected=3, name="write")
        fd_argument, buffer_argument, count_argument = arguments
        self.emit_register_from_argument(argument=buffer_argument, register="si")
        self.emit_register_from_argument(argument=count_argument, register="cx")
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self.emit("        mov ah, SYS_IO_WRITE")
        self.emit("        int 30h")
        self.ax_clear()

    def can_auto_pin(self, *, following_statement: Node | None, statement: VarDecl) -> bool:
        """Decide whether *statement* should be auto-pinned to a register."""
        if len(self.pinned_register) >= len(self.REGISTER_POOL):
            return False
        init = statement.init
        if init is None:
            return True
        # Call initializers stay in memory so they can participate in
        # error-return fusion without clobbering a pin.
        return not isinstance(init, Call)

    @staticmethod
    def check_argument_count(*, arguments: list[Node], expected: int, name: str) -> None:
        """Raise SyntaxError if the argument count doesn't match expected."""
        if expected == 0 and arguments:
            message = f"{name}() takes no arguments"
            raise SyntaxError(message)
        if expected > 0 and len(arguments) != expected:
            message = f"{name}() expects exactly {expected} argument{'s' if expected != 1 else ''}"
            raise SyntaxError(message)

    def check_defined(self, name: str, /) -> None:
        """Raise SyntaxError if a variable is not in scope."""
        if name in self.NAMED_CONSTANTS:
            return
        if name not in self.visible_vars:
            message = f"undefined variable: {name}"
            raise SyntaxError(message)

    def discover_virtual_long_locals(self, statements: list[Node], /) -> None:
        """Identify ``unsigned long`` locals whose DX:AX value can stay live.

        Matches the narrow pattern:

            unsigned long NAME = <long_expr>;
            print_datetime(NAME);

        where ``NAME`` is not referenced anywhere else in the function
        body. Such locals skip the memory slot and the store/load
        round-trip; DX:AX is produced by the initializer and consumed
        directly by the next statement.
        """
        for index in range(len(statements) - 1):
            statement = statements[index]
            if not isinstance(statement, VarDecl):
                continue
            if statement.type_name != "unsigned long" or statement.init is None:
                continue
            consumer = statements[index + 1]
            if not isinstance(consumer, Call) or consumer.name != "print_datetime":
                continue
            if len(consumer.args) != 1:
                continue
            argument = consumer.args[0]
            if not isinstance(argument, Var) or argument.name != statement.name:
                continue
            other_statements = statements[:index] + statements[index + 2:]
            name = statement.name
            if any(self.statement_references(other, name) for other in other_statements):
                continue
            self.virtual_long_locals.add(name)

    def emit(self, line: str = "") -> None:
        """Append a line of assembly to the output buffer."""
        self.lines.append(line)

    def emit_argument_vector_startup(self, parameters: list[Param], /) -> None:
        """Emit inline startup code that parses EXEC_ARG into argc/argv."""
        # Single char* parameter: just load EXEC_ARG directly.
        if len(parameters) == 1 and parameters[0].type == "char*" and not parameters[0].is_array:
            pname = parameters[0].name
            self.emit("        cld")
            self.emit("        mov ax, [EXEC_ARG]")
            self.emit(f"        mov [{self.local_address(pname)}], ax")
            self.ax_is_byte = False
            self.ax_local = pname
            return

        argc_name = None
        argv_name = None
        for param in parameters:
            if param.is_array:
                argv_name = param.name
            elif argc_name is None:
                argc_name = param.name
        if not argv_name:
            return

        self.needs_argv_buf = True
        self.emit("        cld")
        self.emit("        mov di, _argv")
        self.emit("        call FUNCTION_PARSE_ARGV")
        if argc_name:
            self.emit(f"        mov [{self.local_address(argc_name)}], cx")
        self.emit(f"        mov word [{self.local_address(argv_name)}], _argv")

    def emit_binary_operator_operands(self, left: Node, right: Node, /) -> None:
        """Generate left into AX and right into CX.

        When the right operand is a constant or variable, loads it
        directly into CX without a push/pop round-trip.
        """
        if isinstance(right, Int):
            self.generate_expression(left)
            self.emit(f"        mov cx, {right.value}")
        elif isinstance(right, Var) and right.name in self.pinned_register:
            self.generate_expression(left)
            self.emit(f"        mov cx, {self.pinned_register[right.name]}")
        elif isinstance(right, Var) and right.name in self.locals:
            self.generate_expression(left)
            self.emit(f"        mov cx, [{self.local_address(right.name)}]")
        else:
            self.generate_expression(left)
            self.emit("        push ax")
            self.generate_expression(right)
            self.emit("        mov cx, ax")
            self.emit("        pop ax")

    def emit_comparison(self, left: Node, right: Node, /) -> None:
        """Generate a comparison, leaving flags set for a conditional jump.

        Optimizes comparisons against integer constants by using
        ``cmp ax, imm`` directly, and ``test ax, ax`` for zero.  Pinned
        register variables compare against constants in place, skipping
        the load into AX.
        """
        if isinstance(right, Int):
            if isinstance(left, Var) and left.name in self.pinned_register:
                register = self.pinned_register[left.name]
                if right.value == 0:
                    self.emit(f"        test {register}, {register}")
                else:
                    self.emit(f"        cmp {register}, {right.value}")
                return
            self.generate_expression(left)
            if right.value == 0:
                self.emit("        test al, al" if self.ax_is_byte else "        test ax, ax")
            else:
                register = "al" if self.ax_is_byte else "ax"
                self.emit(f"        cmp {register}, {right.value}")
        else:
            self.emit_binary_operator_operands(left, right)
            self.emit("        cmp ax, cx")

    def emit_condition(self, *, condition: Node, context: str) -> str:
        """Validate a condition, emit a comparison, and return the operator.

        Raises:
            SyntaxError: If the condition is not a comparison.

        """
        if not isinstance(condition, BinOp) or condition.op not in JUMP_WHEN_FALSE:
            message = f"{context} condition must be a comparison, got {condition}"
            raise SyntaxError(message)
        if condition.op in ("==", "!="):
            self.validate_equality_types(condition.left, condition.right)
        self.emit_comparison(condition.left, condition.right)
        return condition.op

    def emit_register_from_argument(self, *, argument: Node, register: str) -> None:
        """Load an argument into a specific 16-bit register.

        Handles pinned variables, memory locals, named constants,
        integer literals, and general expressions (evaluated via AX).
        """
        if isinstance(argument, Int) or (isinstance(argument, Var) and argument.name in self.NAMED_CONSTANTS):
            self.emit(f"        mov {register}, {argument.value if isinstance(argument, Int) else argument.name}")
        elif isinstance(argument, Var) and argument.name in self.pinned_register:
            source = self.pinned_register[argument.name]
            if source != register:
                self.emit(f"        mov {register}, {source}")
        elif isinstance(argument, Var) and argument.name == self.ax_local:
            if register != "ax":
                self.emit(f"        mov {register}, ax")
        elif isinstance(argument, Var) and argument.name in self.locals:
            self.emit(f"        mov {register}, [{self.local_address(argument.name)}]")
        elif isinstance(argument, String):
            self.emit(f"        mov {register}, {self.new_string_label(argument.content)}")
        else:
            self.generate_expression(argument)
            if register != "ax":
                self.emit(f"        mov {register}, ax")

    def emit_si_from_argument(self, argument: Node, /) -> None:
        """Load a string or expression argument into SI."""
        if isinstance(argument, String):
            self.emit(f"        mov si, {self.new_string_label(argument.content)}")
        else:
            self.generate_expression(argument)
            self.emit("        mov si, ax")

    def emit_store_local(self, *, expression: Node, name: str) -> None:
        """Generate an expression and store the result in a local variable.

        When ``name`` is pinned to a register, the value is written to
        that register instead of the memory frame.  Constant
        initializers — integers, string literals, or named kernel
        constants — are moved directly into the pinned register
        without going through AX, so the caller's AX tracking (e.g.
        ``arg`` left by the argv startup) survives the store.
        """
        if self.variable_types.get(name) == "unsigned long":
            self.ax_clear()
            self.generate_long_expression(expression)
            if name in self.virtual_long_locals:
                self.live_long_local = name
                return
            address = self.local_address(name)
            if self.elide_frame:
                self.emit(f"        mov [{address}], ax")
                self.emit(f"        mov [{address}+2], dx")
            else:
                low_offset = self.locals[name]
                self.emit(f"        mov [bp-{low_offset}], ax")
                self.emit(f"        mov [bp-{low_offset - 2}], dx")
            self.ax_is_byte = False
            self.ax_local = None
            return
        if name in self.pinned_register:
            register = self.pinned_register[name]
            if isinstance(expression, Int):
                if expression.value == 0:
                    self.emit(f"        xor {register}, {register}")
                else:
                    self.emit(f"        mov {register}, {expression.value}")
                return
            if isinstance(expression, String):
                label = self.new_string_label(expression.content)
                self.emit(f"        mov {register}, {label}")
                return
            if isinstance(expression, Var) and expression.name in self.NAMED_CONSTANTS:
                self.emit(f"        mov {register}, {expression.name}")
                return
        self.generate_expression(expression)
        if name in self.pinned_register:
            register = self.pinned_register[name]
            self.emit(f"        mov {register}, ax")
        else:
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

    def fuse_trailing_printf(self, body: list[Node], /) -> list[Node]:
        """Transform trailing simple printf() calls into die() for main.

        Handles both a direct trailing ``printf(msg)`` and ``printf(msg)``
        at the end of branches in a trailing if-else chain.
        """
        if not body:
            return body
        last = body[-1]
        if self.is_simple_printf(last):
            return [*body[:-1], Call("die", last.args)]
        if isinstance(last, If):
            transformed = self.transform_if_printf(last)
            if transformed is not last:
                return [*body[:-1], transformed]
        return body

    def generate(self, ast: Node, /) -> str:
        """Generate assembly for an entire program AST.

        Returns:
            The complete assembly source as a string.

        """
        self.emit("        org 0600h")
        self.emit()
        self.emit('%include "constants.asm"')
        self.emit()
        for function in ast.functions:
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
        return "\n".join(self.lines) + "\n"

    def generate_body(self, statements: list[Node], /, *, scoped: bool = False) -> None:
        """Generate code for a sequence of statements.

        When *scoped* is True, variables declared inside the block are
        removed from ``visible_vars`` when the block ends.

        Applies several fusions:
        - ``printf(msg); exit();`` → ``die(msg)`` (when msg has no ``%``)
        - ``int err = syscall(...); if (err == 0) { exit(); }`` →
          syscall with ``jnc FUNCTION_EXIT`` (skip error-code conversion)
        - ``int err = syscall(...); if (err != 0) { die(msg); }`` →
          syscall with pre-loaded SI and ``jc FUNCTION_DIE`` (skip sbb)
        - ``if (cond) { die(msg); }`` → pre-load SI and emit a direct
          conditional jump to ``FUNCTION_DIE``, skipping the if-body dance
        """
        saved = self.visible_vars.copy() if scoped else None
        i = 0
        while i < len(statements):
            statement = statements[i]
            # Fuse simple printf() + exit() into die().
            if self.is_simple_printf(statement) and i + 1 < len(statements) and statements[i + 1] == Call("exit", []):
                self.builtin_die(statement.args)
                i += 2
                continue
            # Fuse die-on-error syscall + if-(non)zero-die.
            init = statement.init if isinstance(statement, VarDecl) else None
            # Fuse `if (cond) { die(msg); }` into pre-load SI+CX + jCC .die.
            # AX tracking is preserved because the die path doesn't fall
            # through — the continuation path sees AX unchanged from before.
            if isinstance(statement, If) and statement.else_body is None and len(statement.body) == 1:
                inner = statement.body[0]
                if isinstance(inner, Call) and inner.name == "die" and isinstance(statement.cond, BinOp) and statement.cond.op in JUMP_WHEN_FALSE:
                    die_message = inner.args[0]
                    die_label = self.new_string_label(die_message.content)
                    die_length = string_byte_length(die_message.content)
                    self.emit(f"        mov si, {die_label}")
                    self.emit(f"        mov cx, {die_length}")
                    operator = self.emit_condition(condition=statement.cond, context="if")
                    true_jump = JUMP_INVERT[JUMP_WHEN_FALSE[operator]]
                    self.emit(f"        {true_jump} FUNCTION_DIE")
                    i += 1
                    continue
            # Fuse error-returning syscall + if-zero-exit.
            if init is not None and isinstance(init, Call) and init.name in self.ERROR_RETURNING_BUILTINS and i + 1 < len(statements):
                next_stmt = statements[i + 1]
                skip = 0
                if self.is_zero_exit_if(next_stmt):
                    skip = 2  # skip declaration + if-zero-exit
                elif isinstance(next_stmt, If) and not self.is_zero_test(next_stmt.cond):
                    skip = 1  # skip declaration only, process if-else normally
                if skip:
                    self.visible_vars.add(statement.name)
                    call_node = statement.init
                    handler = getattr(self, f"builtin_{call_node.name}")
                    clobbers = self.BUILTIN_CLOBBERS.get(call_node.name)
                    if self.register_cache and clobbers:
                        self.auto_spill(clobbers=clobbers)
                    handler(call_node.args, fuse_exit=True)
                    self.ax_is_byte = True
                    self.ax_local = statement.name
                    i += skip
                    continue
            self.generate_statement(statement)
            i += 1
        if saved is not None:
            self.visible_vars = saved

    def generate_call(self, statement: Call, /) -> None:
        """Generate code for a function call statement.

        Raises:
            SyntaxError: If the called function is not a known builtin.

        """
        name = statement.name
        arguments = statement.args
        handler = getattr(self, f"builtin_{name}", None)
        if handler is None:
            message = f"unknown builtin: {name}"
            raise SyntaxError(message)
        clobbers = self.BUILTIN_CLOBBERS.get(name)
        if self.register_cache and clobbers:
            self.auto_spill(clobbers=clobbers)
        handler(arguments)

    def generate_do_while(self, statement: DoWhile, /) -> None:
        """Generate assembly for a do...while loop.

        The body executes unconditionally once, then the condition is
        tested at the bottom.  ``break`` inside the body jumps to the
        end label, same as in a ``while`` loop.
        """
        condition, body = statement.cond, statement.body
        label_index = self.new_label()
        end_label = f".do_{label_index}_end"
        self.emit(f".do_{label_index}:")
        self.loop_end_labels.append(end_label)
        self.generate_body(body, scoped=True)
        operator = self.emit_condition(condition=condition, context="do_while")
        # Jump back to top when condition is TRUE (invert the false-jump)
        true_jump = {"!=": "jne", "<": "jl", "<=": "jle", ">": "jg", ">=": "jge", "==": "je"}
        self.emit(f"        {true_jump[operator]} .do_{label_index}")
        self.emit(f"{end_label}:")
        self.loop_end_labels.pop()

    def generate_expression(self, expression: Node, /) -> None:
        """Generate code for an expression, leaving the result in AX.

        Raises:
            SyntaxError: If an unknown expression kind or operator is encountered.

        """
        # Skip load if AX already holds this variable's value.
        if isinstance(expression, Var) and expression.name == self.ax_local:
            return
        if isinstance(expression, Int):
            self.ax_clear()
            self.emit(f"        mov ax, {expression.value}")
        elif isinstance(expression, String):
            self.ax_clear()
            self.emit(f"        mov ax, {self.new_string_label(expression.content)}")
        elif isinstance(expression, Var):
            vname = expression.name
            if vname in self.NAMED_CONSTANTS:
                self.emit(f"        mov ax, {vname}")
                self.ax_clear()
                return
            self.check_defined(vname)
            if self.variable_types.get(vname) == "unsigned long":
                message = f"'unsigned long' variable {vname!r} cannot be used in a 16-bit expression context"
                raise SyntaxError(message)
            if vname in self.pinned_register:
                self.emit(f"        mov ax, {self.pinned_register[vname]}")
            else:
                self.emit(f"        mov ax, [{self.local_address(vname)}]")
            self.ax_is_byte = False
            self.ax_local = vname
        elif isinstance(expression, Index):
            self.ax_clear()
            vname = expression.name
            index_expression = expression.index
            self.check_defined(vname)
            if isinstance(index_expression, Int) and vname in self.array_labels:
                offset = index_expression.value * 2
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
            elif isinstance(index_expression, Int):
                is_byte = vname not in self.variable_arrays and self.variable_types.get(vname) in ("char", "char*")
                offset = index_expression.value * (1 if is_byte else 2)
                if vname in self.pinned_register:
                    self.emit(f"        mov bx, {self.pinned_register[vname]}")
                else:
                    self.emit(f"        mov bx, [{self.local_address(vname)}]")
                if is_byte:
                    if offset:
                        self.emit(f"        mov al, [bx+{offset}]")
                    else:
                        self.emit("        mov al, [bx]")
                    self.emit("        xor ah, ah")
                elif offset:
                    self.emit(f"        mov ax, [bx+{offset}]")
                else:
                    self.emit("        mov ax, [bx]")
            else:
                is_byte = vname not in self.variable_arrays and self.variable_types.get(vname) in ("char", "char*")
                if vname in self.pinned_register:
                    self.emit(f"        mov bx, {self.pinned_register[vname]}")
                else:
                    self.emit(f"        mov bx, [{self.local_address(vname)}]")
                self.emit("        push bx")
                self.generate_expression(index_expression)
                if not is_byte:
                    self.emit("        add ax, ax")
                self.emit("        pop bx")
                self.emit("        add bx, ax")
                if is_byte:
                    self.emit("        mov al, [bx]")
                    self.emit("        xor ah, ah")
                else:
                    self.emit("        mov ax, [bx]")
        elif isinstance(expression, SizeofType):
            self.ax_clear()
            self.emit(f"        mov ax, {self.TYPE_SIZES.get(expression.type_name, 2)}")
        elif isinstance(expression, SizeofVar):
            self.ax_clear()
            vname = expression.name
            if vname in self.array_sizes:
                size = self.array_sizes[vname] * 2  # word-sized elements
            else:
                size = 2  # all non-array variables are word-sized
            self.emit(f"        mov ax, {size}")
        elif isinstance(expression, Call):
            self.generate_call(expression)
        elif isinstance(expression, BinOp):
            operator, left, right = expression.op, expression.left, expression.right
            if operator == "%" and self.has_remainder(left, right):
                self.emit("        mov ax, dx")
                self.ax_clear()
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
            self.ax_clear()
        else:
            message = f"unknown expression: {type(expression).__name__}"
            raise SyntaxError(message)

    def generate_long_expression(self, expression: Node, /) -> None:
        """Generate code for an ``unsigned long`` expression, leaving the result in DX:AX.

        Only the minimal forms needed by current callers are supported:
        a call to the zero-arg ``datetime()`` builtin, or a reference
        to a local variable of type ``unsigned long``. Anything else
        raises :class:`SyntaxError`.
        """
        if isinstance(expression, Call) and expression.name == "datetime":
            self.generate_call(expression)
            return
        if isinstance(expression, Var):
            vname = expression.name
            if self.variable_types.get(vname) != "unsigned long":
                message = f"expected 'unsigned long' expression, got '{self.variable_types.get(vname, 'int')}' variable {vname!r}"
                raise SyntaxError(message)
            if vname in self.virtual_long_locals:
                if self.live_long_local != vname:
                    message = f"internal: virtual long {vname!r} consumed when not live"
                    raise SyntaxError(message)
                self.live_long_local = None
                return
            address = self.local_address(vname)
            if self.elide_frame:
                self.emit(f"        mov ax, [{address}]")
                self.emit(f"        mov dx, [{address}+2]")
            else:
                low_offset = self.locals[vname]
                self.emit(f"        mov ax, [bp-{low_offset}]")
                self.emit(f"        mov dx, [bp-{low_offset - 2}]")
            self.ax_is_byte = False
            self.ax_local = None
            return
        message = f"unsupported 'unsigned long' expression: {type(expression).__name__}"
        raise SyntaxError(message)

    def generate_function(self, function: Function, /) -> None:
        """Generate assembly for a single function definition."""
        name = function.name
        parameters = function.params
        body = function.body
        self.array_labels = {}
        self.array_sizes = {}
        self.ax_clear()
        self.elide_frame = name == "main"
        self.frame_size = 0
        self.live_long_local = None
        self.locals = {}
        self.pinned_register = {}
        self.register_cache = {}
        self.spill_stack = []
        self.variable_arrays = set()
        self.variable_types = {}
        self.virtual_long_locals = set()

        # Allocate parameters as locals and record their types.
        for param in parameters:
            self.allocate_local(param.name)
            self.variable_types[param.name] = param.type
            if param.is_array:
                self.variable_arrays.add(param.name)

        self.discover_virtual_long_locals(body)
        self.scan_locals(body)

        # Seed visible_vars with parameters and pinned variables.
        # Block-scoped locals become visible when their declaration
        # is reached during code generation.
        for param in parameters:
            self.visible_vars.add(param.name)
        self.visible_vars.update(self.pinned_register)

        self.emit(f"{name}:")
        if not self.elide_frame and self.frame_size > 0:
            self.emit("        push bp")
            self.emit("        mov bp, sp")
            self.emit(f"        sub sp, {self.frame_size}")

        # Emit argc/argv startup for main with parameters.
        if name == "main" and parameters:
            self.emit_argument_vector_startup(parameters)

        # Fuse trailing printf() calls into die() since main exits implicitly.
        if name == "main":
            body = self.fuse_trailing_printf(body)
        self.generate_body(body)

        if name == "main":
            self.emit("        jmp FUNCTION_EXIT")
            if self.elide_frame:
                for vname in sorted(self.locals):
                    directive = "dd 0" if self.variable_types.get(vname) == "unsigned long" else "dw 0"
                    self.emit(f"_l_{vname}: {directive}")
        else:
            if self.frame_size > 0:
                self.emit("        mov sp, bp")
                self.emit("        pop bp")
            self.emit("        ret")
        self.emit()

    def generate_if(self, statement: If, /) -> None:
        """Generate assembly for an if statement."""
        condition, body, else_body = statement.cond, statement.body, statement.else_body
        label_index = self.new_label()
        operator = self.emit_condition(condition=condition, context="if")
        saved_ax = (self.ax_local, self.ax_is_byte)
        if else_body is not None:
            self.emit(f"        {JUMP_WHEN_FALSE[operator]} .if_{label_index}_else")
            self.generate_body(body, scoped=True)
            if_exits = self.always_exits(body)
            if not if_exits:
                self.emit(f"        jmp .if_{label_index}_end")
            self.emit(f".if_{label_index}_else:")
            # On the else path, AX is unchanged (comparison doesn't modify it).
            self.ax_local, self.ax_is_byte = saved_ax
            self.generate_body(else_body, scoped=True)
            if not if_exits or not self.always_exits(else_body):
                self.emit(f".if_{label_index}_end:")
            self.ax_clear()
        else:
            self.emit(f"        {JUMP_WHEN_FALSE[operator]} .if_{label_index}_end")
            self.generate_body(body, scoped=True)
            self.emit(f".if_{label_index}_end:")
            # If the body always exits its enclosing block (via die,
            # exit, return, or break), AX is unchanged on the
            # fall-through path.
            if self.always_exits(body):
                self.ax_local, self.ax_is_byte = saved_ax
            else:
                self.ax_clear()

    def generate_statement(self, statement: Node, /) -> None:
        """Generate assembly for a single statement.

        Raises:
            SyntaxError: If an unknown statement kind is encountered.

        """
        if isinstance(statement, VarDecl):
            self.visible_vars.add(statement.name)
            self.variable_types[statement.name] = statement.type_name
            if statement.init is not None:
                self.emit_store_local(expression=statement.init, name=statement.name)
        elif isinstance(statement, ArrayDecl):
            self.visible_vars.add(statement.name)
            self.variable_types[statement.name] = statement.type_name
            if statement.init is not None and isinstance(statement.init, ArrayInit):
                elem_labels = []
                for elem in statement.init.elements:
                    if isinstance(elem, String):
                        elem_labels.append(self.new_string_label(elem.content))
                    elif isinstance(elem, Int):
                        elem_labels.append(str(elem.value))
                    else:
                        message = "array initializer elements must be constants"
                        raise SyntaxError(message)
                array_label = f"_arr_{len(self.arrays)}"
                self.arrays.append((array_label, elem_labels))
                self.array_labels[statement.name] = array_label
                self.array_sizes[statement.name] = len(elem_labels)
                self.emit(f"        mov word [{self.local_address(statement.name)}], {array_label}")
        elif isinstance(statement, Assign):
            self.emit_store_local(expression=statement.expr, name=statement.name)
        elif isinstance(statement, Break):
            if not self.loop_end_labels:
                message = "break outside of a loop"
                raise SyntaxError(message)
            self.emit(f"        jmp {self.loop_end_labels[-1]}")
        elif isinstance(statement, DoWhile):
            self.ax_clear()
            self.generate_do_while(statement)
        elif isinstance(statement, If):
            self.generate_if(statement)
        elif isinstance(statement, While):
            self.ax_clear()
            self.generate_while(statement)
        elif isinstance(statement, Call):
            self.generate_call(statement)
            self.ax_clear()
        else:
            message = f"unknown statement: {type(statement).__name__}"
            raise SyntaxError(message)

    def generate_while(self, statement: While, /) -> None:
        """Generate assembly for a while loop.

        ``while (1)`` and other statically-nonzero conditions skip the
        header check entirely.  The end label is still emitted so a
        ``break`` statement inside the body has a target; when no
        ``break`` is present the label is dead and costs nothing.
        """
        condition, body = statement.cond, statement.body
        label_index = self.new_label()
        end_label = f".while_{label_index}_end"
        self.emit(f".while_{label_index}:")
        self.loop_end_labels.append(end_label)
        if self.is_constant_true_condition(condition):
            self.generate_body(body, scoped=True)
        else:
            operator = self.emit_condition(condition=condition, context="while")
            self.emit(f"        {JUMP_WHEN_FALSE[operator]} {end_label}")
            self.generate_body(body, scoped=True)
        self.emit(f"        jmp .while_{label_index}")
        self.emit(f"{end_label}:")
        self.loop_end_labels.pop()

    def has_remainder(self, left: Node, right: Node, /) -> bool:
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
            and isinstance(right, Int)
            and self.is_modulo_of(base=left, expression=remainder_left)
            and remainder_left.right.value % right.value == 0
        )

    def index_cache_key(self, expression: Node, /) -> tuple[str, int] | None:
        """Return the register cache key for an index expression, or None."""
        if isinstance(expression, Index) and isinstance(expression.index, Int) and expression.name in self.array_labels:
            return (self.array_labels[expression.name], expression.index.value * 2)
        return None

    @staticmethod
    def is_constant_true_condition(condition: Node, /) -> bool:
        """Return True if *condition* is statically nonzero.

        ``parse_condition`` wraps bare expressions as ``expr != 0``,
        so ``while (1)`` reaches here as
        ``BinOp("!=", Int(1), Int(0))``.
        """
        if not isinstance(condition, BinOp) or condition.op != "!=":
            return False
        if condition.right != Int(0):
            return False
        return isinstance(condition.left, Int) and condition.left.value != 0

    @staticmethod
    def is_modulo_of(*, base: Node, expression: Node) -> bool:
        """Check if expression is (base % N) for some integer N."""
        return (
            isinstance(expression, BinOp)
            and expression.op == "%"
            and expression.left == base
            and isinstance(expression.right, Int)
        )

    @staticmethod
    def is_simple_printf(node: Node, /) -> bool:
        """Return True if *node* is ``printf(<literal with no '%'>)``.

        Such calls are semantically equivalent to a plain string print and
        can be folded into ``die()`` at end-of-main / end-of-branch.
        """
        return (
            isinstance(node, Call)
            and node.name == "printf"
            and len(node.args) == 1
            and isinstance(node.args[0], String)
            and "%" not in node.args[0].content
        )

    @staticmethod
    def is_zero_exit_if(statement: Node, /) -> bool:
        """Check if a statement is ``if (VAR == 0) { exit(); }``."""
        return (
            isinstance(statement, If)
            and isinstance(statement.cond, BinOp)
            and statement.cond.op == "=="
            and statement.cond.right == Int(0)
            and len(statement.body) == 1
            and statement.body[0] == Call("exit", [])
            and statement.else_body is None
        )

    @staticmethod
    def is_zero_test(condition: Node, /) -> bool:
        """Check if a condition tests ``VAR == 0``."""
        return isinstance(condition, BinOp) and condition.op == "==" and condition.right == Int(0)

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
        """Allocate a string literal and return its label name.

        Identical strings are deduplicated — subsequent calls with the
        same *content* return the existing label.
        """
        for label, existing in self.strings:
            if existing == content:
                return label
        label = f"_str_{len(self.strings)}"
        self.strings.append((label, content))
        return label

    def peephole(self) -> None:
        """Run peephole optimization passes over generated assembly."""
        self.peephole_dead_code()
        self.peephole_double_jump()
        self.peephole_jump_next()
        self.peephole_label_forwarding()
        self.peephole_store_reload()
        self.peephole_memory_arithmetic()
        self.peephole_constant_to_register()
        self.peephole_dead_ah()
        self.peephole_unused_cld()
        self.peephole_dead_stores()
        self.peephole_dead_test_after_sbb()

    def peephole_constant_to_register(self) -> None:
        """Fold ``mov ax, imm / mov <reg>, ax`` into a direct load.

        Replaces the two-instruction load with ``mov <reg>, imm`` or,
        when the constant is zero, ``xor <reg>, <reg>`` (one byte
        shorter).
        """
        registers = {"bx", "cx", "dx", "si", "di", "bp"}
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if not a.startswith("mov ax, "):
                i += 1
                continue
            immediate = a[len("mov ax, ") :]
            if immediate.startswith("[") or immediate in registers:
                i += 1
                continue
            if not b.startswith("mov "):
                i += 1
                continue
            parts = b[len("mov ") :].split(", ")
            if len(parts) != 2 or parts[1] != "ax" or parts[0] not in registers:
                i += 1
                continue
            register = parts[0]
            if immediate == "0":
                self.lines[i] = f"        xor {register}, {register}"
            else:
                self.lines[i] = f"        mov {register}, {immediate}"
            del self.lines[i + 1]
            continue

    def peephole_dead_ah(self) -> None:
        """Drop ``xor ah, ah`` when the next instruction writes AH.

        The zero-extension after ``mov al, [mem]`` is dead when the
        following statement immediately loads a new value into AH.
        """
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == "xor ah, ah" and b.startswith("mov ah, "):
                del self.lines[i]
                continue
            i += 1

    def peephole_dead_code(self) -> None:
        """Remove unreachable instructions after unconditional jumps."""
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("jmp ") and not b.endswith(":") and ":" not in b:
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

    def peephole_dead_test_after_sbb(self) -> None:
        """Drop ``test ax, ax`` immediately after ``sbb ax, ax``.

        The sbb produces 0 (CF clear) or -1 (CF set) and already
        sets ZF correctly, so the compiler's follow-up test is dead.
        """
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == "sbb ax, ax" and b == "test ax, ax":
                del self.lines[i + 1]
                continue
            i += 1

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

    def peephole_label_forwarding(self) -> None:
        """Retarget jumps through a label that immediately trampolines.

        When an unreachable-by-fall-through label ``.L1:`` is followed
        by ``jmp .L2``, rewrite every ``jCC .L1`` in the rest of the
        function to ``jCC .L2`` and drop the label/jmp pair.  "No
        fall-through" is proven by requiring the previous line to be
        an unconditional ``jmp`` — that's the shape ``break`` at the
        end of a ``while (1)`` body produces right before the
        implicit program exit.
        """
        jumps = {
            "ja",
            "jae",
            "jb",
            "jbe",
            "jc",
            "je",
            "jg",
            "jge",
            "jl",
            "jle",
            "jmp",
            "jnc",
            "jne",
            "jno",
            "jnp",
            "jns",
            "jnz",
            "jo",
            "jp",
            "js",
            "jz",
        }
        i = 1
        while i < len(self.lines) - 1:
            previous_line = self.lines[i - 1].strip()
            label_line = self.lines[i].strip()
            next_line = self.lines[i + 1].strip()
            if not previous_line.startswith("jmp "):
                i += 1
                continue
            if not (label_line.endswith(":") and " " not in label_line):
                i += 1
                continue
            if not next_line.startswith("jmp "):
                i += 1
                continue
            old_label = label_line[:-1]
            new_target = next_line[len("jmp ") :]
            if old_label == new_target:
                i += 1
                continue
            for j in range(len(self.lines)):
                if j == i or j == i + 1:
                    continue
                stripped = self.lines[j].strip()
                parts = stripped.split(None, 1)
                if len(parts) == 2 and parts[0] in jumps and parts[1] == old_label:
                    self.lines[j] = self.lines[j].replace(old_label, new_target)
            del self.lines[i : i + 2]
            i = max(1, i - 1)

    def peephole_memory_arithmetic(self) -> None:
        """Fuse load/modify/store sequences into direct arithmetic.

        Handles these patterns where ``D`` is either a memory operand
        ``[L]`` or a 16-bit general-purpose register:
        - ``mov ax, D / mov cx, 1 / add ax, cx / mov D, ax`` →
          ``inc D`` (or ``inc word [L]`` for memory)
        - ``mov ax, D / mov cx, 1 / sub ax, cx / mov D, ax`` →
          ``dec D`` (or ``dec word [L]`` for memory)
        - ``mov ax, D / mov cx, imm / (add|sub) ax, cx /
          mov D, ax`` → ``(add|sub) D, imm``
        """
        registers = {"bx", "cx", "dx", "si", "di", "bp"}
        i = 0
        while i < len(self.lines) - 3:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            if not a.startswith("mov ax, "):
                i += 1
                continue
            source = a[len("mov ax, ") :]
            is_memory = source.startswith("[") and source.endswith("]")
            is_register = source in registers
            if not (is_memory or is_register):
                i += 1
                continue
            if not (b.startswith("mov cx, ") and not b.endswith("]")):
                i += 1
                continue
            if c not in {"add ax, cx", "sub ax, cx"}:
                i += 1
                continue
            if d != f"mov {source}, ax":
                i += 1
                continue
            immediate = b[len("mov cx, ") :]
            operator = "add" if c == "add ax, cx" else "sub"
            width = "word " if is_memory else ""
            if immediate == "1":
                instruction = "inc" if operator == "add" else "dec"
                self.lines[i] = f"        {instruction} {width}{source}"
            else:
                self.lines[i] = f"        {operator} {width}{source}, {immediate}"
            del self.lines[i + 1 : i + 4]
            continue

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

    def peephole_unused_cld(self) -> None:
        """Remove ``cld`` when no string instruction is emitted.

        The argv parser and single-char* startup emit ``cld`` for
        legacy reasons but never issue ``lodsb``/``stosb``/``rep``.
        """
        string_ops = ("lodsb", "lodsw", "stosb", "stosw", "movsb", "movsw", "scasb", "scasw", "cmpsb", "cmpsw", "rep ")
        for line in self.lines:
            stripped = line.strip()
            if any(stripped.startswith(op) for op in string_ops):
                return
        self.lines = [line for line in self.lines if line.strip() != "cld"]

    def scan_locals(self, statements: list[Node], /, *, top_level: bool = True) -> None:
        """Recursively find variable declarations.

        Plain ``int`` declarations are auto-pinned to a CPU register
        (from :data:`REGISTER_POOL`, in declaration order) when a slot
        is still available.  Call initializers stay in memory so they
        can participate in error-fusion optimizations without
        clobbering a pin.
        """
        for index, statement in enumerate(statements):
            if isinstance(statement, VarDecl):
                self.variable_types[statement.name] = statement.type_name
                if top_level and statement.type_name != "unsigned long":
                    following = statements[index + 1] if index + 1 < len(statements) else None
                    if self.can_auto_pin(following_statement=following, statement=statement):
                        self.pinned_register[statement.name] = self.REGISTER_POOL[len(self.pinned_register)]
                        continue
                if statement.name in self.virtual_long_locals:
                    continue
                size = self.TYPE_SIZES.get(statement.type_name, 2)
                self.allocate_local(statement.name, size=size)
            elif isinstance(statement, ArrayDecl):
                self.variable_types[statement.name] = statement.type_name
                self.allocate_local(statement.name)
            elif isinstance(statement, If):
                self.scan_locals(statement.body, top_level=False)
                if statement.else_body is not None:
                    self.scan_locals(statement.else_body, top_level=False)
            elif isinstance(statement, (DoWhile, While)):
                self.scan_locals(statement.body, top_level=False)

    def statement_references(self, node: Node, name: str, /) -> bool:
        """Return True if ``node`` reads or writes a variable named ``name``."""
        if isinstance(node, Var) and node.name == name:
            return True
        if isinstance(node, Assign) and node.name == name:
            return True
        for field in node.__slots__:
            value = getattr(node, field)
            if isinstance(value, Node):
                if self.statement_references(value, name):
                    return True
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Node) and self.statement_references(item, name):
                        return True
        return False

    def transform_branch_printf(self, body: list[Node], /) -> list[Node]:
        """Replace trailing simple printf(msg) with die(msg) in a branch body."""
        if body and self.is_simple_printf(body[-1]):
            return [*body[:-1], Call("die", body[-1].args)]
        return body

    def transform_if_printf(self, statement: If, /) -> If:
        """Transform simple printf() at end of if-else branches into die()."""
        condition, if_body, else_body = statement.cond, statement.body, statement.else_body
        new_if = self.transform_branch_printf(if_body)
        new_else = else_body
        if else_body is not None:
            if len(else_body) == 1 and isinstance(else_body[0], If):
                transformed = self.transform_if_printf(else_body[0])
                if transformed is not else_body[0]:
                    new_else = [transformed]
            else:
                new_else = self.transform_branch_printf(else_body)
        if new_if is if_body and new_else is else_body:
            return statement
        return If(condition, new_if, new_else)

    def type_of_operand(self, node: Node, /) -> str:
        """Classify an operand for equality type-checking.

        Returns one of: ``"pointer"``, ``"null"``, ``"char"``,
        ``"integer"``, or ``"unknown"`` (expressions whose type we
        cannot statically determine — treated as integers for the
        purposes of the check).
        """
        if isinstance(node, Char):
            return "char"
        if isinstance(node, Index):
            if self.variable_types.get(node.name) in ("char", "char*"):
                return "char"
            return "unknown"
        if isinstance(node, Var):
            if node.name == "NULL":
                return "null"
            variable_type = self.variable_types.get(node.name)
            if variable_type == "char*":
                return "pointer"
            if variable_type == "char":
                return "char"
            if node.name in self.variable_types or node.name in self.NAMED_CONSTANTS:
                return "integer"
        return "unknown"

    def validate_equality_types(self, left: Node, right: Node, /) -> None:
        """Ensure ``==``/``!=`` operands have compatible types.

        Pointers may only be compared to other pointers or ``NULL``;
        ``NULL`` may only appear opposite a pointer; ``char`` values
        must be compared against other ``char`` values or character
        literals (so ``c != 0`` is rejected — use ``c != '\\0'``).
        Comparing a pointer to a non-``NULL`` integer (``if (p == 0)``)
        is a common C bug, so the compiler requires the explicit
        ``NULL`` spelling.
        """
        left_type = self.type_of_operand(left)
        right_type = self.type_of_operand(right)
        if left_type == "pointer" and right_type not in ("pointer", "null"):
            message = f"pointer compared to non-pointer: {left} vs {right}"
            raise SyntaxError(message)
        if right_type == "pointer" and left_type not in ("pointer", "null"):
            message = f"pointer compared to non-pointer: {left} vs {right}"
            raise SyntaxError(message)
        if left_type == "null" and right_type not in ("pointer", "null"):
            message = f"NULL compared to non-pointer: {left} vs {right}"
            raise SyntaxError(message)
        if right_type == "null" and left_type not in ("pointer", "null"):
            message = f"NULL compared to non-pointer: {left} vs {right}"
            raise SyntaxError(message)
        if left_type == "char" and right_type not in ("char", "unknown"):
            message = f"char compared to non-char: {left} vs {right}"
            raise SyntaxError(message)
        if right_type == "char" and left_type not in ("char", "unknown"):
            message = f"char compared to non-char: {left} vs {right}"
            raise SyntaxError(message)


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

    def parse_additive(self) -> Node:
        """Parse an additive expression (addition and subtraction).

        Returns:
            An AST node for the additive expression.

        """
        node = self.parse_multiplicative()
        while self.peek()[0] in ADDITIVE_OPERATORS:
            operator_token = self.eat()
            right = self.parse_multiplicative()
            node = BinOp(operator_token[1], node, right)
        return node

    def parse_arguments(self) -> list[Node]:
        """Parse a comma-separated argument list through the closing paren.

        Returns:
            A list of AST expression nodes.

        """
        arguments: list[Node] = []
        if self.peek()[0] != "RPAREN":
            arguments.append(self.parse_expression())
            while self.peek()[0] == "COMMA":
                self.eat("COMMA")
                arguments.append(self.parse_expression())
        self.eat("RPAREN")
        return arguments

    def parse_array_init(self) -> Node:
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
        return ArrayInit(elems)

    def parse_assignment(self) -> Node:
        """Parse a simple assignment statement.

        Returns:
            An AST node for the assignment.

        """
        name = self.eat("IDENT")[1]
        self.eat("ASSIGN")
        expression = self.parse_expression()
        self.eat("SEMI")
        return Assign(name, expression)

    def parse_block(self) -> list[Node]:
        """Parse statements until a closing brace and consume it.

        Returns:
            A list of AST statement nodes.

        """
        body: list[Node] = []
        while self.peek()[0] != "RBRACE":
            body.append(self.parse_statement())
        self.eat("RBRACE")
        return body

    def parse_call_statement(self) -> Node:
        """Parse a function call statement.

        Returns:
            An AST node for the call statement.

        """
        name = self.eat("IDENT")[1]
        self.eat("LPAREN")
        arguments = self.parse_arguments()
        self.eat("SEMI")
        return Call(name, arguments)

    def parse_comparison(self) -> Node:
        """Parse a comparison expression.

        Returns:
            An AST node for the comparison expression.

        """
        left = self.parse_additive()
        if self.peek()[0] in COMPARISON_OPERATORS:
            operator_token = self.eat()
            right = self.parse_additive()
            return BinOp(operator_token[1], left, right)
        return left

    def parse_compound_assignment(self) -> Node:
        """Parse a compound assignment (+=) statement.

        Returns:
            An AST node for the desugared assignment.

        """
        name = self.eat("IDENT")[1]
        self.eat("PLUS_ASSIGN")
        expression = self.parse_expression()
        self.eat("SEMI")
        # Desugar: i += expr  →  i = i + expr
        return Assign(name, BinOp("+", Var(name), expression))

    def parse_condition(self) -> Node:
        """Parse an if/while condition.

        Wraps a bare expression as ``expr != 0`` so that ``if (error)``
        is equivalent to ``if (error != 0)``.  Comparisons at the top
        level are returned unchanged.

        Returns:
            A BinOp AST node suitable for conditional jumps.

        """
        expression = self.parse_expression()
        if isinstance(expression, BinOp) and expression.op in JUMP_WHEN_FALSE:
            return expression
        return BinOp("!=", expression, Int(0))

    def parse_do_while(self) -> Node:
        """Parse a do...while loop statement.

        Returns:
            A ``DoWhile`` AST node.

        """
        self.eat("DO")
        self.eat("LBRACE")
        body = self.parse_block()
        self.eat("WHILE")
        self.eat("LPAREN")
        condition = self.parse_condition()
        self.eat("RPAREN")
        self.eat("SEMI")
        return DoWhile(condition, body)

    def parse_expression(self) -> Node:
        """Parse an expression.

        Returns:
            An AST node for the expression.

        """
        return self.parse_comparison()

    def parse_function(self) -> Node:
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
        return Function(name, parameters, self.parse_block())

    def parse_if(self) -> Node:
        """Parse an if statement.

        Returns:
            An AST node for the if statement.

        """
        self.eat("IF")
        self.eat("LPAREN")
        condition = self.parse_condition()
        self.eat("RPAREN")
        self.eat("LBRACE")
        body = self.parse_block()
        else_body: list[Node] | None = None
        if self.peek()[0] == "ELSE":
            self.eat("ELSE")
            if self.peek()[0] == "IF":
                else_body = [self.parse_if()]
            else:
                self.eat("LBRACE")
                else_body = self.parse_block()
        return If(condition, body, else_body)

    def parse_multiplicative(self) -> Node:
        """Parse a multiplicative expression (multiplication and division).

        Returns:
            An AST node for the multiplicative expression.

        """
        node = self.parse_primary()
        while self.peek()[0] in MULTIPLICATIVE_OPERATORS:
            operator_token = self.eat()
            right = self.parse_primary()
            node = BinOp(operator_token[1], node, right)
        return node

    def parse_parameter(self) -> Param:
        """Parse a single function parameter.

        Returns:
            A Param dataclass.

        """
        type_string = self.parse_type()
        name = self.eat("IDENT")[1]
        is_array = False
        if self.peek()[0] == "LBRACKET":
            self.eat("LBRACKET")
            self.eat("RBRACKET")
            is_array = True
        return Param(type_string, name, is_array)

    def parse_parameters(self) -> list[Param]:
        """Parse a function parameter list.

        Returns:
            A list of Param dataclasses.

        """
        if self.peek()[0] == "RPAREN":
            return []
        parameters = [self.parse_parameter()]
        while self.peek()[0] == "COMMA":
            self.eat("COMMA")
            parameters.append(self.parse_parameter())
        return parameters

    def parse_primary(self) -> Node:
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
            return Int(int(token[1], 0))
        if token[0] == "CHAR_LIT":
            self.eat()
            return Char(decode_first_character(token[1][1:-1]))
        if token[0] == "STRING":
            self.eat()
            return String(token[1][1:-1])
        if token[0] == "IDENT":
            self.eat()
            if self.peek()[0] == "LPAREN":
                self.eat("LPAREN")
                return Call(token[1], self.parse_arguments())
            if self.peek()[0] == "LBRACKET":
                self.eat("LBRACKET")
                index = self.parse_expression()
                self.eat("RBRACKET")
                return Index(token[1], index)
            return Var(token[1])
        if token[0] == "NOT":
            self.eat()
            return BinOp("==", self.parse_primary(), Int(0))
        if token[0] == "LPAREN":
            self.eat()
            expression = self.parse_expression()
            self.eat("RPAREN")
            return expression
        message = f"line {token[2]}: expected expression, got {token[0]} ({token[1]!r})"
        raise SyntaxError(message)

    def parse_program(self) -> Node:
        """Parse the entire program as a sequence of function declarations.

        Returns:
            An AST node for the program.

        """
        functions = []
        while self.peek()[0] != "EOF":
            functions.append(self.parse_function())
        return Program(functions)

    def parse_sizeof(self) -> Node:
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
            return SizeofType(type_string)
        name = self.eat("IDENT")[1]
        self.eat("RPAREN")
        return SizeofVar(name)

    def parse_statement(self) -> Node:
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
        if token[0] == "BREAK":
            self.eat("BREAK")
            self.eat("SEMI")
            return Break()
        if token[0] == "DO":
            return self.parse_do_while()
        if token[0] == "RETURN":
            self.eat("RETURN")
            self.eat("SEMI")
            return Call("exit", [])
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
        """Parse a type specifier (void, int, char, char*, unsigned long).

        Returns:
            The type as a string.

        Raises:
            SyntaxError: If an unexpected token is encountered, or a bare
                ``long`` / ``unsigned`` without ``long`` appears.

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
        if token[0] == "UNSIGNED":
            self.eat()
            if self.peek()[0] != "LONG":
                following = self.peek()
                message = f"line {token[2]}: only 'unsigned long' is supported, got 'unsigned {following[1]}'"
                raise SyntaxError(message)
            self.eat()
            return "unsigned long"
        if token[0] == "LONG":
            message = f"line {token[2]}: bare 'long' is not supported; use 'unsigned long'"
            raise SyntaxError(message)
        message = f"line {token[2]}: expected type, got {token[0]} ({token[1]!r})"
        raise SyntaxError(message)

    def parse_variable_declaration(self) -> Node:
        """Parse a variable or array declaration.

        Returns:
            An AST node for the declaration.

        """
        type_string = self.parse_type()
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
            return ArrayDecl(name, type_string, init)
        return VarDecl(name, type_string, init)

    def parse_while(self) -> Node:
        """Parse a while loop statement.

        Returns:
            An AST node for the while loop.

        """
        self.eat("WHILE")
        self.eat("LPAREN")
        condition = self.parse_condition()
        self.eat("RPAREN")
        self.eat("LBRACE")
        return While(condition, self.parse_block())

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


def string_byte_length(text: str) -> int:
    r"""Return the byte length of a C string literal, excluding the trailing null.

    Handles escape sequences (``\n``, ``\0``, ``\t``, etc.).
    The string in the AST is the raw content between quotes, e.g.
    ``Hello\n\0`` which decodes to 7 bytes (H e l l o LF NUL)
    but the printable length is 6 (excluding the trailing NUL).
    """
    length = 0
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            i += 2  # escape sequence = 1 decoded byte
        else:
            i += 1
        length += 1
    # Subtract the trailing \0 if present
    if text.endswith("\\0"):
        length -= 1
    return length


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
