#!/usr/bin/env python3
"""Minimal C subset compiler for BBoeOS.

Compiles a tiny subset of C to NASM-compatible assembly that the BBoeOS
self-hosted assembler (or host NASM) can assemble into a flat binary.

Grammar:
    program              := (function_declaration | global_declaration)*
    function_declaration := type IDENT '(' parameters? ')' '{' statement* '}'
    global_declaration   := type IDENT ('[' expression? ']')?
                          ('=' (expression | '{' expression_list '}'))? ';'
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
                          |  IDENT ('+='|'&='|'|='|'^='|'<<='|'>>=') expression ';'
    do_while             := 'do' '{' statement* '}' 'while' '(' expression ')' ';'
    while                := 'while' '(' expression ')' '{' statement* '}'
    call_statement       := IDENT '(' arguments ')' ';'
    arguments            := expression (',' expression)*
    expression           := logical_or_expression
    logical_or_expression := logical_and_expression ('||' logical_and_expression)*
    logical_and_expression := bitwise_or_expression ('&&' bitwise_or_expression)*
    bitwise_or_expression := bitwise_xor_expression ('|' bitwise_xor_expression)*
    bitwise_xor_expression := bitwise_and_expression ('^' bitwise_and_expression)*
    bitwise_and_expression := comparison_expression ('&' comparison_expression)*
    comparison_expression := shift_expression
                           (('<'|'>'|'<='|'>='|'=='|'!=')
                            shift_expression)?
    shift_expression     := additive_expression (('<<'|'>>') additive_expression)*
    additive_expression  := multiplicative_expression
                           (('+'|'-') multiplicative_expression)*
    multiplicative_expression := primary (('*'|'/'|'%') primary)*
    primary              := NUMBER | STRING | sizeof | '~' primary
                          | IDENT ('(' arguments ')' | '[' expression ']')?
                          | '(' expression ')'

Preprocessor:
    ``#define NAME VALUE`` — object-like macro, substituted at tokenization.
    ``#include "path"`` — NASM ``%include`` semantics: double-quoted only,
    resolved relative to the including file's directory, recursively
    expanded, cycles rejected.

Builtins:
    asm(literal)             -- emit NASM source verbatim (escape hatch;
                                works at statement and file scope)
    checksum(buf, len)       -- 1's-complement checksum (for ICMP/IP)
    close(fd)                -- close a file descriptor
    datetime()               -- return unsigned seconds since 1970-01-01 UTC
    die(message)             -- print message and terminate
    exec(name)               -- run program; on failure returns ERROR_* code
    exit()                   -- terminate program
    mkdir(name)              -- create directory, return 0 or ERR_* code
    net_open(type)           -- open socket (SOCK_RAW or SOCK_DGRAM), return fd or -1
    open(name, flags)        -- open file, return fd or -1 on error
    print_datetime(epoch)    -- print epoch as YYYY-MM-DD HH:MM:SS
    putchar(expression)      -- print single character
    read(fd, buffer, count)  -- read bytes from fd, return count or -1
    reboot()                 -- warm-reboot the machine (does not return)
    recvfrom(fd, buf, len, port) -- receive UDP datagram filtered by port
    sendto(fd, buf, len, ip, src_port, dst_port) -- send UDP datagram
    set_exec_arg(ptr)        -- pass argument pointer to the next exec()
    shutdown()               -- APM power off (returns only on failure)
    sleep(milliseconds)      -- busy-wait for the given duration
    ticks()                  -- return low 16 bits of BIOS timer tick counter
    uptime()                 -- return seconds since boot
    write(fd, buffer, count) -- write bytes to fd, return count or -1

Usage: cc.py <input.c> [output.asm]
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Callable


class CompileError(Exception):
    """Raised for user-visible compilation errors.

    The optional ``line`` attribute lets :func:`main` format the
    diagnostic with a source line number without a Python traceback.
    """

    def __init__(self, message: str, /, *, line: int | None = None) -> None:
        """Store the message and optional line number."""
        self.message = message
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


@dataclass(slots=True)
class Node:
    """Base class for every AST node.

    ``line`` is the 1-based source line where the construct begins; it
    defaults to 0 for nodes synthesized by the compiler (e.g. constant
    folding) and is set by the parser to the first token's line for
    everything else.  The field is keyword-only so subclasses can keep
    their positional constructors, and excluded from ``__eq__`` so two
    AST nodes with the same shape compare equal regardless of source
    location — several peephole / fusion passes rely on structural
    equality (``cond.right == Int(0)`` etc.).
    """

    line: int = field(default=0, kw_only=True, compare=False)


@dataclass(slots=True)
class Param:
    """A function parameter: type, name, and whether it was declared with ``[]``."""

    type: str
    name: str
    is_array: bool


@dataclass(slots=True)
class ArrayDecl(Node):
    """Array declaration ``T name[] = {...};`` (local or global).

    At global scope the array may also carry an explicit ``[SIZE]`` with
    no initializer — stored as ``size`` (a parser node, evaluated at
    NASM assemble time so it can reference kernel constants).
    """

    name: str
    type_name: str
    init: Node | None
    size: Node | None = field(default=None, kw_only=True)


@dataclass(slots=True)
class ArrayInit(Node):
    """Brace-initializer ``{a, b, c}`` for an array declaration."""

    elements: list[Node]


@dataclass(slots=True)
class Assign(Node):
    """Assignment ``name = expr;`` or ``name += expr;`` (the latter lowers to ``name = name + expr``)."""

    name: str
    expr: Node


@dataclass(slots=True)
class BinOp(Node):
    """Binary operator expression ``left OP right``."""

    op: str
    left: Node
    right: Node


@dataclass(slots=True)
class Break(Node):
    """``break;`` statement (exits the innermost loop)."""


@dataclass(slots=True)
class Call(Node):
    """Function/builtin call ``name(args...)``."""

    name: str
    args: list[Node]


@dataclass(slots=True)
class Continue(Node):
    """``continue;`` statement (jumps to the innermost loop's condition test)."""


@dataclass(slots=True)
class DoWhile(Node):
    """``do { body } while (cond);`` loop."""

    cond: Node
    body: list[Node]


@dataclass(slots=True)
class Function(Node):
    """Function definition: name, parameter list, and body.

    ``regparm_count`` captures the ``__attribute__((regparm(N)))``
    annotation on the definition (0 = standard cdecl, 1 = first arg
    arrives in AX and is spilled to a local stack slot in the
    prologue).  Only regparm(1) is currently supported.

    ``carry_return`` captures ``__attribute__((carry_return))`` —
    the function reports its int return via the carry flag instead
    of AX: ``return 1`` → ``clc`` then epilogue, ``return 0`` →
    ``stc`` then epilogue.  Callers use it in ``if`` / ``while``
    conditions, where cc.py dispatches on ``jnc`` (true) / ``jc``
    (false) directly — no AX round-trip.

    ``always_inline`` captures ``__attribute__((always_inline))`` —
    the function must have a single ``asm("...")`` body and zero or
    one (regparm(1)) parameter.  At each C-level call site, cc.py
    splices the body text in place of ``call X`` (with local label
    uniquification); no free-standing function body is emitted, so
    there's nothing for inline-asm ``call X`` to resolve against.
    """

    name: str
    params: list[Param]
    body: list[Node]
    regparm_count: int = field(default=0, kw_only=True)
    carry_return: bool = field(default=False, kw_only=True)
    always_inline: bool = field(default=False, kw_only=True)


@dataclass(slots=True)
class If(Node):
    """``if (cond) { body } [else { else_body }]`` statement."""

    cond: Node
    body: list[Node]
    else_body: list[Node] | None


@dataclass(slots=True)
class Index(Node):
    """Subscript expression ``name[index]``."""

    name: str
    index: Node


@dataclass(slots=True)
class IndexAssign(Node):
    """Indexed assignment ``name[index] = expr;``."""

    name: str
    index: Node
    expr: Node


@dataclass(slots=True)
class InlineAsm(Node):
    """File-scope ``asm("...");`` directive.

    The content is the raw string literal text (still carrying C
    escape sequences); ``builtin_asm`` decodes and emits it at tail.
    """

    content: str


@dataclass(slots=True)
class Int(Node):
    """Integer literal."""

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
class LogicalAnd(Node):
    """Short-circuit ``left && right`` expression."""

    left: Node
    right: Node


@dataclass(slots=True)
class LogicalOr(Node):
    """Short-circuit ``left || right`` expression."""

    left: Node
    right: Node


@dataclass(slots=True)
class Program(Node):
    """Top-level AST: functions and file-scope global declarations.

    ``globals`` holds :class:`VarDecl` / :class:`ArrayDecl` nodes
    declared at file scope.  Scalars become ``_g_<name>`` cells in the
    tail data block; arrays become ``_g_<name>`` labels that user code
    references by name just like a local ``int arr[] = {...};``.
    """

    functions: list[Node]
    globals: list[Node] = field(default_factory=list, kw_only=True)


@dataclass(slots=True)
class Return(Node):
    """``return [expr];`` statement."""

    value: Node | None


@dataclass(slots=True)
class SizeofType(Node):
    """``sizeof(type_name)`` expression."""

    type_name: str


@dataclass(slots=True)
class SizeofVar(Node):
    """``sizeof(name)`` expression (size of a declared variable)."""

    name: str


@dataclass(slots=True)
class String(Node):
    """String literal."""

    content: str


@dataclass(slots=True)
class Var(Node):
    """Reference to a named variable or named constant."""

    name: str


@dataclass(slots=True)
class VarDecl(Node):
    """Scalar local declaration ``T name [= init];``.

    ``asm_register`` captures the ``__attribute__((asm_register("REG")))``
    annotation on a file-scope declaration — the declared variable is
    aliased to the named CPU register, so reads compile as the register
    itself (no ``[_g_name]`` load) and writes compile as a direct
    ``mov REG, ...``.  ``None`` for ordinary scalars / locals.
    """

    name: str
    type_name: str
    init: Node | None
    asm_register: str | None = field(default=None, kw_only=True)


@dataclass(slots=True)
class While(Node):
    """``while (cond) { body }`` loop."""

    cond: Node
    body: list[Node]


ADDITIVE_OPERATORS = frozenset({"MINUS", "PLUS"})

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
    # Pseudo-operators for ``carry_return`` call conditions.  CF clear
    # means the call reported ``return 1`` (true); CF set means
    # ``return 0`` (false).  ``if (foo())`` dispatches through
    # ``carry`` (jump-false = ``jc``); ``if (foo() == 0)`` through
    # ``not_carry`` (jump-false = ``jnc``).  No real ``cmp`` runs —
    # the ``call`` itself leaves CF holding the result.
    "carry": "jc",
    "not_carry": "jnc",
}

JUMP_WHEN_TRUE = {
    "!=": "jne",
    "<": "jl",
    "<=": "jle",
    ">": "jg",
    ">=": "jge",
    "==": "je",
    "carry": "jnc",
    "not_carry": "jc",
}

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

TYPE_TOKENS = frozenset({"CHAR", "CONST", "INT", "LONG", "UNSIGNED", "VOID"})


def _ast_contains(node: Node, predicate: Callable[[Node], bool], /) -> bool:
    """Return True if any node in the tree satisfies *predicate*.

    Generic AST walker used by :meth:`CodeGenerator.__name_is_reassigned`,
    :meth:`CodeGenerator.__node_references_var`, and
    :meth:`CodeGenerator.__statement_references`.
    """
    if predicate(node):
        return True
    for node_field in fields(node):
        value = getattr(node, node_field.name)
        if isinstance(value, Node):
            if _ast_contains(value, predicate):
                return True
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Node) and _ast_contains(item, predicate):
                    return True
    return False


class CodeGenerator:
    """Generates NASM x86 assembly from the parsed AST."""

    BUILTIN_CLOBBERS: ClassVar[dict[str, frozenset[str]]] = {
        "asm": frozenset({"ax", "bx", "cx", "dx", "si", "di"}),
        "checksum": frozenset({"ax", "bx", "cx", "si"}),
        "chmod": frozenset({"ax", "si"}),
        "close": frozenset({"ax", "bx"}),
        "datetime": frozenset({"ax", "dx"}),
        "die": frozenset(),
        "exec": frozenset({"ax", "si"}),
        "exit": frozenset(),
        "fstat": frozenset({"ax", "bx", "cx", "dx"}),
        "getchar": frozenset({"ax"}),
        "mac": frozenset({"ax", "di"}),
        "memcpy": frozenset({"ax", "cx", "di", "si"}),
        "mkdir": frozenset({"ax", "si"}),
        "net_open": frozenset({"ax", "dx"}),
        "open": frozenset({"ax", "dx", "si"}),
        "parse_ip": frozenset({"ax", "di", "si"}),
        "print_datetime": frozenset({"ax", "bx", "cx", "dx", "si"}),
        "print_ip": frozenset({"ax", "cx", "si"}),
        "print_mac": frozenset({"ax", "cx", "si"}),
        "printf": frozenset({"ax", "bx", "cx", "dx", "si", "di"}),
        "putchar": frozenset({"ax"}),
        "read": frozenset({"ax", "bx", "cx", "di"}),
        "reboot": frozenset({"ax"}),
        "recvfrom": frozenset({"ax", "bx", "cx", "di", "dx"}),
        "rename": frozenset({"ax", "di", "si"}),
        "sendto": frozenset({"ax", "bx", "cx", "di", "dx", "si"}),
        "set_exec_arg": frozenset({"ax"}),
        "shutdown": frozenset({"ax"}),
        "sleep": frozenset({"ax", "cx"}),
        "strlen": frozenset({"ax", "cx", "di"}),
        "ticks": frozenset({"ax", "cx", "dx"}),
        "uptime": frozenset({"ax"}),
        "video_mode": frozenset({"ax"}),
        "write": frozenset({"ax", "bx", "cx", "si"}),
    }

    ERROR_RETURNING_BUILTINS: ClassVar[frozenset[str]] = frozenset({"chmod", "mac", "mkdir", "parse_ip", "rename"})

    #: Identifiers that resolve to NASM kernel constants rather than
    #: user-defined variables.  Emitted verbatim so NASM can resolve
    #: them from ``constants.asm``.
    NAMED_CONSTANTS: ClassVar[frozenset[str]] = frozenset({
        "ARGV",
        "arp_frame",
        "BUFFER",
        "DIRECTORY_ENTRY_SIZE",
        "DIRECTORY_NAME_LENGTH",
        "DIRECTORY_OFFSET_FLAGS",
        "EDIT_BUFFER_BASE",
        "EDIT_BUFFER_SIZE",
        "EDIT_KILL_BUFFER",
        "EDIT_KILL_BUFFER_SIZE",
        "ERROR_EXISTS",
        "ERROR_NOT_EXECUTE",
        "ERROR_NOT_FOUND",
        "ERROR_PROTECTED",
        "EXEC_ARG",
        "FLAG_DIRECTORY",
        "FLAG_EXECUTE",
        "IPPROTO_ICMP",
        "IPPROTO_UDP",
        "MAX_INPUT",
        "NULL",
        "O_CREAT",
        "O_RDONLY",
        "O_TRUNC",
        "O_WRONLY",
        "register_table",
        "SECTOR_BUFFER",
        "SOCK_DGRAM",
        "SOCK_RAW",
        "STDERR",
        "STDIN",
        "STDOUT",
        "STR_ASSIGN",
        "STR_BYTE",
        "STR_DB",
        "STR_DD",
        "STR_DEFINE",
        "STR_DW",
        "STR_EQU",
        "STR_INCLUDE",
        "STR_ORG",
        "STR_SHORT",
        "STR_TIMES",
        "STR_WORD",
        "VIDEO_MODE_CGA_320x200",
        "VIDEO_MODE_CGA_640x200",
        "VIDEO_MODE_EGA_320x200_16",
        "VIDEO_MODE_EGA_640x200_16",
        "VIDEO_MODE_EGA_640x350_16",
        "VIDEO_MODE_TEXT_40x25",
        "VIDEO_MODE_TEXT_80x25",
        "VIDEO_MODE_VGA_320x200_256",
        "VIDEO_MODE_VGA_640x480_16",
    })

    #: Named constants that, when referenced, require a NASM %include
    #: directive in the generated output to provide their symbol.
    NAMED_CONSTANT_INCLUDES: ClassVar[dict[str, str]] = {
        "arp_frame": "arp_frame.asm",
    }

    #: Registers available for auto-pinning, in allocation order.
    REGISTER_POOL: ClassVar[tuple[str, ...]] = ("dx", "cx", "bx", "di")

    TYPE_SIZES: ClassVar[dict[str, int]] = {
        # ``char`` locals share the word-sized store/load codepaths
        # the rest of codegen uses (``mov [addr], ax`` / ``mov ax,
        # [addr]`` plus ``xor ah, ah``), so each char gets a full
        # word.  Switching to byte slots would require a parallel
        # ``mov byte`` / ``mov al`` codepath in every emitter — not
        # done because the savings are small (mainly the ``xor ah,
        # ah`` zero-extend) and the duplication isn't worth it.
        "char": 2,
        "char*": 2,
        "int": 2,
        "unsigned long": 4,
        "void": 0,
    }

    def __init__(self, *, defines: dict[str, str] | None = None) -> None:
        """Initialize code generator state.

        ``defines`` is the ``#define`` table the preprocessor collected.
        Each entry is re-emitted as a NASM ``%define NAME VALUE`` at the
        top of the output so inline-asm strings (which cc.py does not
        scan for C macros) can reference the same symbolic names that
        C code uses — otherwise every use inside an ``asm(...)`` string
        would have to spell the literal.
        """
        self.array_labels: dict[str, str] = {}
        self.array_sizes: dict[str, int] = {}
        self.arrays: list[tuple[str, list[str]]] = []
        self.ax_is_byte: bool = False
        self.ax_local: str | None = None
        self.constant_aliases: dict[str, str] = {}
        self.defines: dict[str, str] = dict(defines) if defines else {}

        self.division_remainder: tuple | None = None
        self.elide_frame: bool = False
        self.frame_size: int = 0
        self.global_arrays: dict[str, ArrayDecl] = {}
        self.global_byte_arrays: set[str] = set()
        self.global_scalars: dict[str, VarDecl] = {}
        self.label_id: int = 0
        self.lines: list[str] = []
        self.live_long_local: str | None = None
        self.locals: dict[str, int] = {}
        self.loop_continue_labels: list[str] = []
        self.loop_end_labels: list[str] = []
        self.pinned_register: dict[str, str] = {}
        self.required_includes: set[str] = set()
        self.store_target_register: str | None = None
        self.strings: list[tuple[str, str]] = []
        self.user_functions: dict[str, int] = {}  # name → param count
        self.fastcall_functions: set[str] = set()  # regparm(1) callees: arg 0 in AX
        self.carry_return_functions: set[str] = set()  # callees that return via CF
        self.inline_bodies: dict[str, str] = {}  # always_inline callees: name → raw asm body
        self.inline_call_counter: int = 0  # per-inline-site label-uniquification suffix
        self.register_aliased_globals: dict[str, str] = {}  # name → register (e.g. "si")
        self.variable_arrays: set[str] = set()
        self.variable_types: dict[str, str] = {}
        self.virtual_long_locals: set[str] = set()
        self.visible_vars: set[str] = set()

    def _register_inline_body(self, function: Function, /) -> None:
        """Record an ``always_inline`` function's asm body for splicing.

        The function must have a single ``asm("...")`` statement as its
        entire body.  The raw string (unescaped) is stored; each call
        site pastes it in place of ``call <name>``.  Stack parameters
        are already blocked at parse time (``always_inline`` requires
        0 or regparm(1) params), so callers never need a ``add sp, N``
        cleanup that would fall between the inlined body and the
        following code.
        """
        body = function.body
        if len(body) != 1 or not isinstance(body[0], Call) or body[0].name != "asm":
            message = f"always_inline function '{function.name}' must have a single asm() body"
            raise CompileError(message, line=function.line)
        asm_arg = body[0].args[0]
        if not isinstance(asm_arg, String):
            message = f"always_inline function '{function.name}' asm() body must be a string literal"
            raise CompileError(message, line=function.line)
        self.inline_bodies[function.name] = asm_arg.content

    def _emit_inline_body(self, name: str, /) -> None:
        """Emit the stored body for an ``always_inline`` function.

        Local labels (``.foo:`` / ``.bar:``) are renamed with a
        per-call-site suffix so that multiple inline sites of the
        same function don't produce duplicate labels.  The asm text
        is emitted line-by-line with the same indentation style cc.py
        uses for file-scope inline-asm blocks.
        """
        body = _decode_string_escapes(self.inline_bodies[name])
        self.inline_call_counter += 1
        suffix = f"_inl{self.inline_call_counter}"
        label_pattern = re.compile(r"^\s*(\.\w+)\s*:", re.MULTILINE)
        labels = {match.group(1) for match in label_pattern.finditer(body)}
        for label in labels:
            new_label = f"{label}{suffix}"
            body = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])", new_label, body)
        self.emit(f";; --- inline {name} ---")
        for line in body.splitlines():
            if line:
                self.emit(line if line.startswith((" ", "\t", ".")) else f"        {line}")

    def _analyze_user_function_conventions(self, functions: list[Node], /) -> None:
        """Pre-compute each user function's pinned-param register map.

        Runs the same pin-selection logic that :meth:`generate_function`
        uses, but purely analytically — no code is emitted.  The result
        populates :attr:`user_function_pin_params` so call-site emission
        knows which registers every callee expects.

        A function also qualifies for the register calling convention
        (added to :attr:`register_convention_functions`) when every
        call to it in the program passes only simple arguments (``Int``,
        ``String``, or ``Var``).  Complex-expression arguments would
        require ordering complex eval against register-moves without
        clobbering caller pins, so those callees fall back to the
        stack convention.
        """
        self.user_function_pin_params: dict[str, dict[int, str]] = {}
        self.register_convention_functions: set[str] = set()

        for function in functions:
            if function.name == "main":
                continue
            self.safe_pin_registers = self.compute_safe_pin_registers(function.body)
            # Fastcall (regparm(1)) param 0 lives in AX on entry and is spilled
            # to a local stack slot in the prologue; it never becomes a pin
            # candidate so auto-pin selection skips it entirely.  Params 1..N
            # of a fastcall function keep the standard stack convention in the
            # MVP — they don't mix with register_convention.
            pin_params = function.params[1:] if function.regparm_count > 0 else function.params
            assignments = self._select_auto_pin_candidates(body=function.body, parameters=pin_params)
            param_pins: dict[int, str] = {}
            for index, param in enumerate(function.params):
                if function.regparm_count > 0 and index == 0:
                    continue
                if param.name in assignments:
                    param_pins[index] = assignments[param.name]
            self.user_function_pin_params[function.name] = param_pins

        has_complex_call: dict[str, bool] = dict.fromkeys(self.user_functions, False)

        def visit(node: Node) -> None:
            if isinstance(node, Call) and node.name in self.user_functions and any(not self._is_simple_arg(arg) for arg in node.args):
                has_complex_call[node.name] = True
            for node_field in fields(node):
                value = getattr(node, node_field.name)
                if isinstance(value, Node):
                    visit(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Node):
                            visit(item)

        for function in functions:
            for statement in function.body:
                visit(statement)

        for name, pins in self.user_function_pin_params.items():
            if name in self.fastcall_functions:
                # Fastcall and register_convention are mutually exclusive
                # in the MVP: the former passes arg 0 in AX and the rest
                # via the standard stack; the latter piggybacks on
                # auto-pinned params.  Skip the register_convention
                # promotion so call sites take the fastcall path.
                continue
            if pins and not has_complex_call.get(name):
                self.register_convention_functions.add(name)

    def _arg_pinned_sources(self, arg: Node, /) -> set[str]:
        """Return caller-pinned registers read while evaluating *arg*.

        Used by :meth:`_emit_register_arg_moves` to schedule arg loads
        without overwriting a register that another arg still needs.
        Walks ``Var``/``BinOp`` recursively; non-leaf nodes outside
        the simple-arg shape contribute no sources (and would be
        rejected by :meth:`_is_simple_arg` upstream anyway).
        """
        if isinstance(arg, Var):
            if arg.name in self.pinned_register:
                return {self.pinned_register[arg.name]}
            return set()
        if isinstance(arg, BinOp):
            return self._arg_pinned_sources(arg.left) | self._arg_pinned_sources(arg.right)
        return set()

    @staticmethod
    def _byte_index_base_key(node: Index, /) -> str:
        """Return a string key identifying the base pointer of an Index.

        Two byte-index nodes share a base when their keys match,
        meaning a single ``mov bx, <base>`` can serve both.
        """
        return node.name

    def _byte_index_direct(self, node: Index, /) -> str | None:
        """Return a direct NASM memory operand for a constant-base Index.

        When the base is a named constant or constant alias, returns
        e.g. ``"BUFFER+128+12"`` without emitting any instructions.
        Returns ``None`` for runtime (non-constant) bases.
        """
        vname = node.name
        const_base = self._resolve_constant(vname)
        if const_base is None:
            return None
        offset = node.index.value
        return f"{const_base}+{offset}" if offset else const_base

    @staticmethod
    def _check_argument_count(*, arguments: list[Node], expected: int, name: str) -> None:
        """Raise CompileError if the argument count doesn't match expected.

        Derives the line number from the first argument when available,
        so the diagnostic points at the call site even though the
        builtin handlers are invoked with only the argument list.
        """
        line = arguments[0].line if arguments else None
        if expected == 0 and arguments:
            message = f"{name}() takes no arguments"
            raise CompileError(message, line=line)
        if expected > 0 and len(arguments) != expected:
            message = f"{name}() expects exactly {expected} argument{'s' if expected != 1 else ''}"
            raise CompileError(message, line=line)

    def _check_defined(self, name: str, /, *, line: int | None = None) -> None:
        """Raise CompileError if a variable is not in scope."""
        if name in self.NAMED_CONSTANTS:
            return
        if name not in self.visible_vars:
            message = f"undefined variable: {name}"
            raise CompileError(message, line=line)

    def _collect_constant_references(self, node: Node, /) -> set[str]:
        """Return every NAMED_CONSTANT name referenced inside *node*.

        Used by callers of :meth:`_constant_expression` to register
        ``%include`` requirements for constants that need them.  Only
        descends through the same node shapes :meth:`_constant_expression`
        accepts, so the result matches what was actually inlined.
        """
        if isinstance(node, Var) and node.name in self.NAMED_CONSTANTS:
            return {node.name}
        if isinstance(node, BinOp):
            return self._collect_constant_references(node.left) | self._collect_constant_references(node.right)
        return set()

    def _constant_expression(self, init: Node, /) -> str | None:
        """Return a NASM constant expression if *init* is compile-time resolvable.

        Recognizes integer literals, ``NAMED_CONSTANT`` references,
        constant aliases, and arbitrarily-nested ``+``/``-``/``*``
        combinations of those.  Returns a NASM expression string (e.g.
        ``"BUFFER"``, ``"(BUFFER+128)"``, or
        ``"((O_WRONLY+O_CREAT)+O_TRUNC)"``) that the assembler folds at
        link time, or ``None`` if any leaf is not constant.
        """
        if isinstance(init, Int):
            return str(init.value)
        if isinstance(init, Var):
            if init.name in self.NAMED_CONSTANTS:
                return init.name
            if init.name in self.constant_aliases:
                return self.constant_aliases[init.name]
            return None
        if isinstance(init, BinOp) and init.op in ("+", "-", "*", "&", "|", "^"):
            left = self._constant_expression(init.left)
            right = self._constant_expression(init.right)
            if left is not None and right is not None:
                return f"({left}{init.op}{right})"
        return None

    def _dispatch_chain_var(self, statement: If, /) -> str | None:
        """Return the local var name shared by an if-else dispatch chain.

        A chain is two or more nested ``if (var op literal) … else if
        (var op literal) …`` clauses on the same memory-resident
        local, where each comparison is one of ``==``/``!=``/``<``/
        ``<=``/``>``/``>=`` and the variable always sits on the left.
        Pinned vars, constant aliases, and array bases are excluded —
        their compares already avoid the memory operand.

        Returns the variable's name when hoisting it into AX would
        let two or more comparisons collapse to ``cmp ax, imm``.
        Returns ``None`` for unrelated ifs and single comparisons
        (where the hoist would only break even).
        """
        target: str | None = None
        chain_length = 0
        current: Node | None = statement
        while isinstance(current, If):
            condition = current.cond
            if not (isinstance(condition, BinOp) and condition.op in ("==", "!=", "<", "<=", ">", ">=")):
                break
            if not (isinstance(condition.left, Var) and isinstance(condition.right, Int)):
                break
            name = condition.left.name
            if not self._is_memory_scalar(name):
                break
            if name in self.pinned_register or name in self.constant_aliases or name in self.variable_arrays:
                break
            if self.variable_types.get(name) == "unsigned long":
                break
            if target is None:
                target = name
            elif target != name:
                break
            chain_length += 1
            else_body = current.else_body
            if else_body is None or len(else_body) != 1:
                break
            current = else_body[0]
        return target if chain_length >= 2 else None

    def _emit_byte_index_si(self, node: Index, /) -> tuple[str, bool]:
        """Load the base pointer of a byte-indexed node into SI.

        Returns ``(operand, guarded)`` where *operand* is the NASM
        memory operand (e.g. ``byte [si+12]`` or ``byte [si]``)
        suitable for use in a ``cmp`` instruction, and *guarded* is
        True when an SI-scratch guard (``push si``) was emitted —
        callers must pair it with :meth:`_si_scratch_guard_end` after
        the operand is consumed, else SI = aliased-source_cursor gets
        clobbered by the base load.  Prefers direct addressing when
        the base is a constant (no guard needed).
        """
        direct = self._byte_index_direct(node)
        if direct is not None:
            return (f"byte [{direct}]", False)
        vname = node.name
        offset = node.index.value
        guarded = self._si_scratch_guard_begin(vname)
        self._emit_load_var(vname, register="si")
        operand = f"byte [si+{offset}]" if offset else "byte [si]"
        return (operand, guarded)

    def _si_scratch_guard_begin(self, base_var: str | None = None, /) -> bool:
        """Emit ``push si`` if SI is aliased to a pinned global.

        When an ``asm_register("si")`` global is declared, SI holds the
        aliased value across the program.  Subscripts on other ``char
        *`` pointers normally lower to ``mov si, <base> ; mov al,
        [si]``, which would trash the alias.  This helper emits a
        ``push si`` guard (returns True) when:

        - an ``asm_register("si")`` global exists, and
        - the subscript base *isn't* that same global (no clobber
          happens when ``mov si, si`` is a no-op).

        The caller pairs the guard with :meth:`_si_scratch_guard_end`.
        """
        if not any(register == "si" for register in self.register_aliased_globals.values()):
            return False
        if base_var is not None and self.register_aliased_globals.get(base_var) == "si":
            return False
        self.emit("        push si")
        return True

    def _si_scratch_guard_end(self, *, guarded: bool) -> None:
        """Pair with :meth:`_si_scratch_guard_begin` — emit ``pop si``."""
        if guarded:
            self.emit("        pop si")

    def _emit_constant_base_index_addr(
        self,
        *,
        const_base: str,
        index: Node,
        is_byte: bool,
        preserve_ax: bool,
    ) -> str:
        """Set up ``[CONST + disp + si]`` addressing for a constant-base index.

        Folds a trailing ``±Int`` off a ``Var ± Int`` index into the
        displacement so ``buf[gap_start - 1]`` becomes
        ``[EDIT_BUFFER_BASE-1+si]`` after a single
        ``mov si, [_l_gap_start]``.  Byte-indexed references skip the
        load entirely when the index variable is pinned to DI or BX
        (``[CONST+di]`` / ``[CONST+bx]`` are valid 8086 addressing);
        BP-pinned vars don't qualify because BP would resolve through
        SS, not DS, and CX/DX aren't general index registers in real
        mode either.  This BX/DI restriction is what
        :meth:`_select_auto_pin_candidates` reads via
        ``index_uses`` to keep heavily-subscripted vars off BP.

        When *preserve_ax* is True, any path that evaluates the index
        through AX pushes/pops AX so the caller's value survives.
        """
        element_size = 1 if is_byte else 2
        displacement = 0
        if isinstance(index, BinOp) and index.op in ("+", "-") and isinstance(index.right, Int):
            sign = 1 if index.op == "+" else -1
            displacement = sign * index.right.value * element_size
            index = index.left
        base_register = "si"
        if isinstance(index, Int):
            displacement += index.value * element_size
            self.emit("        xor si, si")
        elif is_byte and isinstance(index, Var) and index.name in self.pinned_register and self.pinned_register[index.name] in ("di", "bx"):
            base_register = self.pinned_register[index.name]
        elif isinstance(index, Var) and index.name in self.pinned_register:
            self.emit(f"        mov si, {self.pinned_register[index.name]}")
            if not is_byte:
                self.emit("        add si, si")
        elif isinstance(index, Var) and self._is_memory_scalar(index.name):
            self.emit(f"        mov si, [{self._local_address(index.name)}]")
            if not is_byte:
                self.emit("        add si, si")
        else:
            if preserve_ax:
                self.emit("        push ax")
            self.generate_expression(index)
            if not is_byte:
                self.emit("        add ax, ax")
            self.emit("        mov si, ax")
            if preserve_ax:
                self.emit("        pop ax")
        addr = const_base
        if displacement != 0:
            addr += f"{displacement:+d}"
        addr += f"+{base_register}"
        return addr

    def _emit_global_storage(self) -> None:
        """Emit ``_g_<name>`` data cells for every global, once at tail.

        Scalars lay out as a single ``dw`` with either the constant
        initializer or zero.  Initialized arrays use ``db``/``dw``
        literals matching the element type.  Uninitialized arrays use
        ``times <byte_count> db 0``; byte-granular output keeps the
        self-hosted assembler happy (it only implements the ``times N
        db ...`` form).  Size expressions can reference named constants
        so NASM folds the arithmetic at assemble time.
        """
        if not self.global_scalars and not self.global_arrays:
            return
        self.emit(";; --- global data ---")
        for name in sorted(self.global_scalars):
            declaration = self.global_scalars[name]
            if name in self.register_aliased_globals:
                # Storage lives in the aliased CPU register, not memory,
                # so no ``_g_<name>`` label is emitted.
                continue
            init_expression = "0"
            if declaration.init is not None:
                init_expression = self._constant_expression(declaration.init)
            self.emit(f"_g_{name}: dw {init_expression}")
        for name in sorted(self.global_arrays):
            declaration = self.global_arrays[name]
            stride = 1 if declaration.type_name == "char" else 2
            if declaration.init is not None:
                directive = "db" if declaration.type_name == "char" else "dw"
                rendered = [
                    self.new_string_label(element.content) if isinstance(element, String) else self._constant_expression(element)
                    for element in declaration.init.elements
                ]
                self.emit(f"_g_{name}: {directive} {', '.join(rendered)}")
            else:
                size_expression = self._constant_expression(declaration.size)
                byte_count = f"({size_expression})*{stride}" if stride != 1 else size_expression
                self.emit(f"_g_{name}: times {byte_count} db 0")

    def _emit_load_var(self, name: str, /, *, register: str = "bx") -> None:
        """Load a variable's value into *register*.

        Checks pinned registers first, then constant aliases, then
        falls back to the memory frame slot.
        """
        if name in self.pinned_register:
            self.emit(f"        mov {register}, {self.pinned_register[name]}")
        elif name in self.register_aliased_globals:
            source = self.register_aliased_globals[name]
            if source != register:
                self.emit(f"        mov {register}, {source}")
        elif name in self.constant_aliases:
            self.emit(f"        mov {register}, {self.constant_aliases[name]}")
        else:
            self.emit(f"        mov {register}, [{self._local_address(name)}]")

    def _emit_syscall(self, name: str, /) -> None:
        """Emit ``mov ah, SYS_<NAME> / int 30h``."""
        self.emit(f"        mov ah, SYS_{name}")
        self.emit("        int 30h")

    @staticmethod
    def _extract_local_label(line: str, /) -> str | None:
        """Return the _l_ label from a store or declaration, or None.

        Stops at the first non-identifier byte so a byte-offset store
        like ``mov [_l_sum+1], al`` still resolves to ``_l_sum`` — the
        same way peephole_dead_stores resolves reads.
        """
        # Store: mov [_l_NAME], ... or mov word [_l_NAME], ...
        if line.startswith("mov") and "[_l_" in line and "], " in line:
            start = line.index("[_l_") + 1
            end = start
            while end < len(line) and (line[end].isalnum() or line[end] == "_"):
                end += 1
            return line[start:end]
        # Declaration: _l_NAME: dw 0
        if line.startswith("_l_") and line.endswith(": dw 0"):
            return line[: line.index(":")]
        return None

    @staticmethod
    def _flatten_and(condition: Node, /) -> list[Node]:
        """Flatten a left-leaning ``&&`` tree into a list of leaves."""
        leaves: list[Node] = []
        while isinstance(condition, LogicalAnd):
            leaves.append(condition.right)
            condition = condition.left
        leaves.append(condition)
        leaves.reverse()
        return leaves

    def _has_remainder(self, left: Node, right: Node, /) -> bool:
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
            and self._is_modulo_of(base=left, expression=remainder_left)
            and remainder_left.right.value % right.value == 0
        )

    def _index_cache_key(self, expression: Node, /) -> tuple[str, int] | None:
        """Return the register cache key for an index expression, or None."""
        if isinstance(expression, Index) and isinstance(expression.index, Int) and expression.name in self.array_labels:
            return (self.array_labels[expression.name], expression.index.value * 2)
        return None

    def _is_byte_eq(self, node: Node, /) -> bool:
        """Check if a node is ``byte_index == <something>``."""
        return isinstance(node, BinOp) and node.op == "==" and self._is_byte_index(node.left)

    def _is_byte_index(self, node: Node, /) -> bool:
        """Check if a node is a constant-subscript byte index."""
        return (
            isinstance(node, Index)
            and isinstance(node.index, Int)
            and node.name not in self.array_labels
            and node.name in self.visible_vars
            and self.variable_types.get(node.name) in ("char", "char*")
        )

    def _is_byte_var(self, name: str, /) -> bool:
        """Return True if *name* is a byte-sized element source.

        Covers plain ``char`` / ``char *`` scalars and file-scope ``char``
        arrays (declared ``char NAME[SIZE];``).  Locally-declared arrays
        and ``int``-typed globals keep word-sized element access — only
        the explicit ``char`` array path widens to byte semantics.
        """
        if name in self.global_byte_arrays:
            return True
        return name not in self.variable_arrays and self.variable_types.get(name) in ("char", "char*")

    def _is_constant_alias(self, *, body: list[Node], statement: VarDecl) -> bool:
        """Check if ``statement`` is a compile-time constant alias.

        True when the local is initialized from a ``NAMED_CONSTANT``
        or ``NAMED_CONSTANT +/- Int`` and never reassigned in ``body``.
        Such locals can be replaced with the underlying constant
        expression directly, skipping the memory slot, the initial
        store, and every reload.
        """
        if statement.type_name == "unsigned long":
            return False
        if self._constant_expression(statement.init) is None:
            return False
        return not any(self._name_is_reassigned(name=statement.name, node=stmt) for stmt in body)

    @staticmethod
    def _is_constant_true_condition(condition: Node, /) -> bool:
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
    def _is_live_after(*, name: str, statements: list[Node]) -> bool:
        """Check if *name* is read before being unconditionally killed.

        Scans *statements* in order.  An unconditional ``Assign`` to
        *name* (whose RHS does not read *name*) kills the old value,
        so any subsequent reads reference the new value, not the one
        from the fuse-die candidate.  Returns False (not live) if
        *name* is never read, or is killed before being read.
        """
        for stmt in statements:
            # Unconditional reassignment kills the old value — but only
            # if the RHS doesn't read the variable (e.g. `err = err + 1`
            # would read the old value).
            if isinstance(stmt, Assign) and stmt.name == name and not CodeGenerator._node_references_var(name=name, node=stmt.expr):
                return False
            if CodeGenerator._node_references_var(name=name, node=stmt):
                return True
        return False

    def _is_memory_scalar(self, name: str, /) -> bool:
        """Return True when *name* is a memory-resident scalar.

        Covers frame-based locals (``self.locals``) and file-scope
        scalars (``self.global_scalars``).  Call sites use this to
        decide whether a ``Var`` can be addressed directly via ``[mem]``
        instead of being loaded into AX first.  Register-aliased globals
        (``asm_register``) report False because their storage is a CPU
        register, not a memory slot.
        """
        if name in self.register_aliased_globals:
            return False
        return name in self.locals or name in self.global_scalars

    @staticmethod
    def _is_modulo_of(*, base: Node, expression: Node) -> bool:
        """Check if expression is (base % N) for some integer N."""
        return isinstance(expression, BinOp) and expression.op == "%" and expression.left == base and isinstance(expression.right, Int)

    @staticmethod
    def _is_simple_arg(node: Node, /) -> bool:
        """Return True if a call argument is safe for the register calling convention.

        "Safe" means :meth:`_emit_register_arg_single` can evaluate it
        without clobbering registers that another arg still needs.
        The base case is ``Int``/``String``/``Var`` (a single ``mov``
        from immediate/memory/pinned-reg).  ``BinOp(+/-, leaf, leaf)``
        is also safe: ``generate_expression`` handles those via the
        ``add ax, [mem]``/``sub ax, imm`` fast paths, which only touch
        AX.  Inter-arg conflicts are checked separately at codegen
        time by the topological ordering in
        :meth:`_emit_register_arg_moves`.
        """
        if isinstance(node, (Int, String, Var)):
            return True
        if isinstance(node, BinOp) and node.op in ("+", "-"):
            return isinstance(node.left, (Int, String, Var)) and isinstance(node.right, (Int, String, Var))
        return False

    @staticmethod
    def _is_simple_printf(node: Node, /) -> bool:
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
    def _is_zero_exit_if(statement: Node, /) -> bool:
        """Check if a statement is ``if (VAR == 0) { exit(); }`` or ``if (VAR == 0) { return ...; }``."""
        return (
            isinstance(statement, If)
            and isinstance(statement.cond, BinOp)
            and statement.cond.op == "=="
            and statement.cond.right == Int(0)
            and len(statement.body) == 1
            and (statement.body[0] == Call("exit", []) or isinstance(statement.body[0], Return))
            and statement.else_body is None
        )

    def _local_address(self, name: str, /) -> str:
        """Return the memory operand string for a local or global scalar.

        Local variables shadow globals with the same name (standard C),
        so the local-frame path runs first and only falls through to
        ``_g_<name>`` when no local slot exists.  Register-aliased
        globals have no memory address — they live in a CPU register —
        so this path raises if called on one (caller should have
        routed through ``register_aliased_globals`` instead).
        """
        if name in self.locals:
            if self.elide_frame:
                return f"_l_{name}"
            offset = self.locals[name]
            if offset > 0:
                return f"bp-{offset}"
            return f"bp+{-offset}"
        if name in self.register_aliased_globals:
            message = f"register-aliased global '{name}' has no memory address"
            raise CompileError(message)
        if name in self.global_scalars:
            return f"_g_{name}"
        message = f"no address for '{name}' (not a local or global scalar)"
        raise CompileError(message)

    @staticmethod
    def _name_is_reassigned(*, name: str, node: Node) -> bool:
        """Return True if an ``Assign(name=name, ...)`` occurs inside ``node``."""
        return _ast_contains(node, lambda n: isinstance(n, Assign) and n.name == name)

    @staticmethod
    def _node_references_var(*, name: str, node: Node) -> bool:
        """Return True if ``Var(name)`` occurs anywhere inside ``node``."""
        return _ast_contains(node, lambda n: isinstance(n, Var) and n.name == name)

    def _emit_push_arg(self, arg: Node, /) -> None:
        """Push a single argument onto the stack, preferring compact forms.

        Immediates, string labels, NAMED_CONSTANTs, constant aliases,
        and pinned-register variables all avoid the ``mov ax, X / push
        ax`` pair.  Any other form falls back to ``generate_expression``
        followed by ``push ax``.
        """
        if isinstance(arg, Int):
            self.emit(f"        push {arg.value}")
        elif isinstance(arg, String):
            label = self.new_string_label(arg.content)
            self.emit(f"        push {label}")
        elif isinstance(arg, Var) and arg.name in self.NAMED_CONSTANTS:
            self.emit_constant_reference(arg.name)
            self.emit(f"        push {arg.name}")
        elif isinstance(arg, Var) and arg.name in self.constant_aliases:
            self.emit(f"        push {self.constant_aliases[arg.name]}")
        elif isinstance(arg, Var) and arg.name in self.global_arrays:
            self.emit(f"        push _g_{arg.name}")
        elif isinstance(arg, Var) and arg.name in self.pinned_register:
            self.emit(f"        push {self.pinned_register[arg.name]}")
        else:
            self.generate_expression(arg)
            self.emit("        push ax")

    def _emit_register_arg_moves(self, register_args: list[tuple[str, Node]], /) -> None:
        """Emit ``mov`` instructions that place args in target registers.

        Each item carries a ``sources`` set of caller-pinned registers
        it reads (``{caller_pin}`` for simple ``Var`` args,
        recursively-collected for ``BinOp`` args, empty otherwise).
        The topological loop picks an item whose target register is
        not in any other item's source set, which guarantees that
        emitting the item won't trash a value another item still
        needs.  When two simple args form a read/write cycle
        (``mov bx, di`` / ``mov di, bx``), the first item's source is
        copied through AX to break it.  ``BinOp`` args participating
        in a cycle would need a stack temp that the current cdecl-
        fallback never has to emit; we raise a ``CompileError`` so
        the caller can be reshaped instead.
        """
        items: list[dict] = []
        for target, arg in register_args:
            sources = self._arg_pinned_sources(arg)
            primary_source: str | None = None
            if isinstance(arg, Var) and arg.name in self.pinned_register:
                primary_source = self.pinned_register[arg.name]
            items.append({"target": target, "arg": arg, "source": primary_source, "sources": sources})
        while items:
            progress_index = None
            for index, item in enumerate(items):
                target = item["target"]
                blocked = any(j != index and target in other["sources"] for j, other in enumerate(items))
                if not blocked:
                    progress_index = index
                    break
            if progress_index is not None:
                item = items.pop(progress_index)
                self._emit_register_arg_single(target=item["target"], arg=item["arg"], source=item["source"])
                continue
            # Cycle break: only the simple-Var case supports the AX
            # spill (the BinOp path can't reroute its operand reads).
            item = items[0]
            if not isinstance(item["arg"], Var) or item["source"] is None:
                message = "register-convention call has a cyclic register dependency that involves a complex argument"
                raise CompileError(message, line=getattr(item["arg"], "line", None))
            source = item["source"]
            self.emit(f"        mov ax, {source}")
            for other in items:
                if source in other["sources"]:
                    other["sources"] = {register if register != source else "ax" for register in other["sources"]}
                    if other["source"] == source:
                        other["source"] = "ax"
                        other["arg"] = None  # mark as "load from ax"

    def _emit_register_arg_single(self, *, target: str, arg: Node, source: str | None) -> None:
        """Emit a single register-arg load for :meth:`_emit_register_arg_moves`.

        *source* is the register currently holding the value to move
        (set when the original ``arg`` was a pinned-register ``Var``
        and may have been redirected to ``ax`` after a cycle break).
        A ``None`` *source* means read directly from the AST node.
        """
        if source is not None:
            if source != target:
                self.emit(f"        mov {target}, {source}")
            return
        if isinstance(arg, Int):
            if arg.value == 0 and target != "ax":
                self.emit(f"        xor {target}, {target}")
            else:
                self.emit(f"        mov {target}, {arg.value}")
        elif isinstance(arg, String):
            label = self.new_string_label(arg.content)
            self.emit(f"        mov {target}, {label}")
        elif isinstance(arg, Var) and arg.name in self.NAMED_CONSTANTS:
            self.emit_constant_reference(arg.name)
            self.emit(f"        mov {target}, {arg.name}")
        elif isinstance(arg, Var) and arg.name in self.constant_aliases:
            self.emit(f"        mov {target}, {self.constant_aliases[arg.name]}")
        elif isinstance(arg, Var) and arg.name in self.global_arrays:
            self.emit(f"        mov {target}, _g_{arg.name}")
        elif isinstance(arg, Var):
            self.emit(f"        mov {target}, [{self._local_address(arg.name)}]")
        elif isinstance(arg, BinOp):
            # ``_is_simple_arg`` only admits BinOp(+/-, leaf, leaf), and
            # the topological scheduler in ``_emit_register_arg_moves``
            # already verified that ``target`` is not read by any other
            # pending arg.  Evaluate into AX, then move into target.
            self.generate_expression(arg)
            if target != "ax":
                self.emit(f"        mov {target}, ax")
        else:
            message = f"register-arg target {target} given unexpected complex node {arg!r}"
            raise CompileError(message, line=getattr(arg, "line", None))

    def _peephole_will_strand_ax(self) -> bool:
        """Return True if the last three emitted lines form a fusion target.

        :meth:`peephole_memory_arithmetic` collapses
        ``mov ax, D / <op> ax, ... / mov D, ax`` into ``<op> D, ...`` when
        source and destination match (passes 2 and 3); :meth:`peephole_register_arithmetic`
        pushes the computation directly into a pin-eligible destination
        register when it differs from the source.  Both leave AX holding
        something other than the new stored value, so the ``ax_local``
        tracking the caller just set (pointing at the store's destination
        local) would mislead later reads into skipping a reload and
        picking up stale contents.

        The caller — :meth:`emit_store_local` — consults this after the
        final ``mov <D>, ax`` has been emitted; if we report True it
        clears its own tracking instead of guessing at peephole time.
        """
        if len(self.lines) < 3:
            return False
        first = self.lines[-3].strip()
        middle = self.lines[-2].strip()
        last = self.lines[-1].strip()
        if not (first.startswith("mov ax, ") and last.startswith("mov ") and last.endswith(", ax")):
            return False
        source = first[len("mov ax, ") :]
        destination = last[len("mov ") : -len(", ax")].strip()
        if source == destination:
            # Passes 2 and 3 of peephole_memory_arithmetic cover inc/dec
            # and (add|sub|and) with any operand shape (imm, register,
            # or ``[mem]``).
            if middle in ("inc ax", "dec ax"):
                return True
            return middle.startswith(("add ax, ", "sub ax, ", "and ax, ", "or ax, ", "xor ax, "))
        # peephole_register_arithmetic: different register destination,
        # op in {add, sub, and, or, xor}, operand doesn't reference the target.
        if destination in {"bx", "cx", "dx", "si", "di", "bp"}:
            for prefix in ("add ax, ", "sub ax, ", "and ax, ", "or ax, ", "xor ax, "):
                if middle.startswith(prefix):
                    operand = middle[len(prefix) :]
                    return destination not in operand.split()
        return False

    def _pinned_registers_to_save(self, clobbers: frozenset[str], /) -> list[str]:
        """Return the pinned registers that need push/pop around a call.

        Order is deterministic (sorted) so ``push`` / ``pop`` pairs
        nest correctly.  ``ax`` is never pinned, so never saved here.
        """
        pinned_set = set(self.pinned_register.values())
        return sorted(register for register in clobbers if register in pinned_set and register != "ax")

    def _register_globals(self, declarations: list[Node], /) -> None:
        """Record file-scope declarations and validate their shapes.

        Scalars are stashed in :attr:`global_scalars`; arrays in
        :attr:`global_arrays`.  ``char`` arrays are additionally tracked
        in :attr:`global_byte_arrays` so :meth:`_is_byte_var` reports
        byte-wide element access (``int`` arrays keep word access).
        """
        for declaration in declarations:
            if isinstance(declaration, InlineAsm):
                continue
            name = declaration.name
            if name in self.NAMED_CONSTANTS:
                message = f"global '{name}' shadows a kernel constant"
                raise CompileError(message, line=declaration.line)
            if name in self.user_functions or name == "main":
                message = f"global '{name}' collides with a function name"
                raise CompileError(message, line=declaration.line)
            if name in self.global_scalars or name in self.global_arrays:
                message = f"duplicate global declaration: {name}"
                raise CompileError(message, line=declaration.line)
            if isinstance(declaration, VarDecl):
                if declaration.type_name == "unsigned long":
                    message = "unsigned long globals are not supported"
                    raise CompileError(message, line=declaration.line)
                if declaration.type_name == "void":
                    message = f"global '{name}' cannot have type void"
                    raise CompileError(message, line=declaration.line)
                if declaration.init is not None and self._constant_expression(declaration.init) is None:
                    message = f"global '{name}' initializer must be a constant expression"
                    raise CompileError(message, line=declaration.line)
                if declaration.init is not None:
                    for constant in self._collect_constant_references(declaration.init):
                        self.emit_constant_reference(constant)
                if declaration.asm_register is not None:
                    if declaration.init is not None:
                        message = f"register-aliased global '{name}' cannot have a constant initializer (initialize from main() instead)"
                        raise CompileError(message, line=declaration.line)
                    self.register_aliased_globals[name] = declaration.asm_register
                self.global_scalars[name] = declaration
            elif isinstance(declaration, ArrayDecl):
                if declaration.type_name not in ("char", "int"):
                    message = f"global array '{name}' must have element type 'char' or 'int'"
                    raise CompileError(message, line=declaration.line)
                if declaration.type_name == "char":
                    self.global_byte_arrays.add(name)
                if declaration.size is not None:
                    if self._constant_expression(declaration.size) is None:
                        message = f"global array '{name}' size must be a constant expression"
                        raise CompileError(message, line=declaration.line)
                    for constant in self._collect_constant_references(declaration.size):
                        self.emit_constant_reference(constant)
                if declaration.init is not None:
                    for element in declaration.init.elements:
                        if isinstance(element, String):
                            continue
                        if self._constant_expression(element) is None:
                            message = "global array initializer elements must be constants"
                            raise CompileError(message, line=element.line)
                        for reference in self._collect_constant_references(element):
                            self.emit_constant_reference(reference)
                self.global_arrays[name] = declaration
            else:
                message = f"unexpected top-level declaration: {type(declaration).__name__}"
                raise CompileError(message, line=declaration.line)

    def _resolve_constant(self, name: str, /) -> str | None:
        """Return the NASM constant expression for *name*, or ``None``.

        Checks :attr:`constant_aliases` first, then
        :attr:`NAMED_CONSTANTS`, then file-scope arrays (whose
        ``_g_<name>`` label is itself a fixed address).  Used wherever
        the code needs to distinguish compile-time-constant bases from
        runtime variables.
        """
        alias = self.constant_aliases.get(name)
        if alias is not None:
            return alias
        if name in self.NAMED_CONSTANTS:
            return name
        if name in self.global_arrays:
            return f"_g_{name}"
        return None

    def _select_auto_pin_candidates(self, *, body: list[Node], parameters: list) -> dict[str, str]:
        """Choose locals/parameters to auto-pin and match them to registers.

        Body locals win slots before parameters — pinning a body local
        lets its initializer target the register directly (avoiding the
        store) and, when every body local fits, eliminates the frame
        allocation entirely.  Within each class, candidates are ranked
        by Var/Assign/Index/IndexAssign occurrence count in *body*,
        with declaration order as the tiebreaker.  The ranked candidate
        list is zipped with :attr:`safe_pin_registers` (already sorted
        by ascending clobber count), so the top candidate gets the
        cheapest register.  A pin is emitted only when the candidate's
        reference count strictly exceeds the matched register's
        clobber count — otherwise the ``push``/``pop`` overhead at each
        clobbering call would swallow the savings.

        Eligibility mirrors :meth:`can_auto_pin` and :meth:`scan_locals`:
        ``unsigned long`` locals, constant aliases, and call-initialized
        locals are skipped; array parameters are skipped as well.

        Returns:
            ``{name: register}`` for each selected pin.  Empty when no
            candidate beats its register's clobber cost.

        """
        if not self.safe_pin_registers:
            return {}

        param_candidates: list[tuple[str, int]] = []
        for order, param in enumerate(parameters):
            if param.is_array:
                continue
            param_candidates.append((param.name, order))

        body_candidates: list[tuple[str, int]] = []
        order = 0

        def collect(nodes: list[Node], *, top_level: bool) -> None:
            nonlocal order
            for statement in nodes:
                if isinstance(statement, VarDecl):
                    eligible = (
                        statement.type_name != "unsigned long"
                        and not (top_level and self._is_constant_alias(body=nodes, statement=statement))
                        and not isinstance(statement.init, Call)
                    )
                    if eligible:
                        body_candidates.append((statement.name, order))
                        order += 1
                if isinstance(statement, If):
                    collect(statement.body, top_level=False)
                    if statement.else_body is not None:
                        collect(statement.else_body, top_level=False)
                elif isinstance(statement, (DoWhile, While)):
                    collect(statement.body, top_level=False)

        collect(body, top_level=True)

        counts: dict[str, int] = {}
        index_uses: dict[str, int] = {}
        # Per-var tallies that drive the expression-temporary check
        # below.  ``ax_resident_uses`` counts Var refs sitting on the
        # LEFT of a comparison whose right side is an integer literal
        # — those are exactly the sites ``emit_comparison`` reuses an
        # AX-resident value for via the ``ax_local`` fast path.  Right
        # operands and non-cmp uses go to ``other_uses`` because the
        # left operand's expression eval clobbers AX before they're
        # reached, so they need either a memory load or a pin.
        ax_resident_uses: dict[str, int] = {}
        other_uses: dict[str, int] = {}
        init_count: dict[str, int] = {}
        init_expr: dict[str, Node] = {}
        comparison_ops = {"==", "!=", "<", "<=", ">", ">="}

        def collect_index_vars(node: Node) -> None:
            """Tally Var occurrences inside Index/IndexAssign subscripts.

            Each subscript pays a 2-byte ``mov si, bp`` penalty when
            its index variable is BP-pinned, since BP can't index
            DS-relative memory in real mode.  The cost-model below
            uses this tally to decide whether a candidate's
            BP-clobber-savings outweigh that per-subscript penalty.
            """
            if isinstance(node, Var):
                index_uses[node.name] = index_uses.get(node.name, 0) + 1
            for node_field in fields(node):
                value = getattr(node, node_field.name)
                if isinstance(value, Node):
                    collect_index_vars(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Node):
                            collect_index_vars(item)

        def count_visit(node: Node, *, role: str = "other") -> None:
            if isinstance(node, (Var, Assign, Index, IndexAssign)):
                counts[node.name] = counts.get(node.name, 0) + 1
            if isinstance(node, VarDecl) and node.init is not None:
                init_count[node.name] = init_count.get(node.name, 0) + 1
                init_expr[node.name] = node.init
                count_visit(node.init)
                return
            if isinstance(node, Assign):
                init_count[node.name] = init_count.get(node.name, 0) + 1
                init_expr[node.name] = node.expr
                count_visit(node.expr)
                return
            if isinstance(node, Var):
                if role == "cmp_left_imm":
                    ax_resident_uses[node.name] = ax_resident_uses.get(node.name, 0) + 1
                else:
                    other_uses[node.name] = other_uses.get(node.name, 0) + 1
            if isinstance(node, BinOp):
                if node.op in comparison_ops:
                    # Only the LEFT operand can reuse an AX-resident
                    # value: the right side is loaded into CX after the
                    # left's evaluation has overwritten AX.  Even on
                    # the left, the fast path requires the right side
                    # to be a constant (Int or NAMED_CONSTANT) so the
                    # cmp can be ``cmp ax, imm`` / ``cmp ax, NAME``.
                    right_is_const = isinstance(node.right, Int) or (
                        isinstance(node.right, Var) and node.right.name in self.NAMED_CONSTANTS
                    )
                    left_role = "cmp_left_imm" if right_is_const else "other"
                    count_visit(node.left, role=left_role)
                    count_visit(node.right, role="other")
                else:
                    count_visit(node.left, role="other")
                    count_visit(node.right, role="other")
                return
            if isinstance(node, (Index, IndexAssign)):
                collect_index_vars(node.index)
            for node_field in fields(node):
                value = getattr(node, node_field.name)
                if isinstance(value, Node):
                    count_visit(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Node):
                            count_visit(item)

        for statement in body:
            count_visit(statement)

        def is_expression_temporary(name: str) -> bool:
            """Skip pinning vars whose value lives in AX between assignment and consumer.

            A var assigned exactly once from a non-trivial expression
            (Call/Index/BinOp — all leave the value in AX) and consumed
            only as the LEFT operand of a comparison against an integer
            literal naturally lives in AX through its lifetime.
            ``emit_comparison``'s fast path emits ``cmp ax, imm`` for
            those uses without re-loading the value, so pinning the
            var would only add a redundant ``mov pin, ax`` after the
            assignment.  Vars used as right-of-cmp or in arithmetic
            still benefit from a pin (the left operand's eval clobbers
            AX before reaching them) so they're left alone here.
            """
            if init_count.get(name, 0) != 1:
                return False
            if other_uses.get(name, 0) != 0:
                return False
            if ax_resident_uses.get(name, 0) == 0:
                return False
            return isinstance(init_expr.get(name), (Call, Index, BinOp))

        def rank(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
            return sorted(items, key=lambda item: (-counts.get(item[0], 0), item[1]))

        combined = rank(body_candidates) + rank(param_candidates)
        # Drop expression-temporary vars: pinning them adds a 2-byte
        # ``mov pin, ax`` after their single complex-expression
        # initializer without shrinking the comparisons that follow
        # (those already work against AX).
        combined = [item for item in combined if not is_expression_temporary(item[0])]
        assignments: dict[str, str] = {}
        available = list(self.safe_pin_registers)
        for name, _ in combined:
            if not available:
                break
            non_bp = [register for register in available if register != "bp"]
            best_other = min(non_bp, key=lambda register: self.register_clobber_counts.get(register, 0)) if non_bp else None
            # Decide BP vs the cheapest non-BP register when both are
            # still available.  BP avoids push/pop at every callee
            # that clobbers ``best_other`` (2 bytes each), but adds a
            # 2-byte ``mov si, bp`` to every subscript reference.
            # Choose whichever side wins by raw byte count.
            if "bp" in available and best_other is not None:
                bp_savings = self.register_clobber_counts.get(best_other, 0)
                bp_penalty = index_uses.get(name, 0)
                chosen = "bp" if bp_savings > bp_penalty else best_other
            elif "bp" in available:
                chosen = "bp"
            elif best_other is not None:
                chosen = best_other
            else:
                break
            refs = counts.get(name, 0)
            if refs > self.register_clobber_counts.get(chosen, 0):
                assignments[name] = chosen
                available.remove(chosen)
            else:
                break
        return assignments

    @staticmethod
    def _statement_references(node: Node, name: str, /) -> bool:
        """Return True if ``node`` reads or writes a variable named ``name``."""
        return _ast_contains(
            node,
            lambda n: (isinstance(n, Var) and n.name == name) or (isinstance(n, Assign) and n.name == name),
        )

    def _transform_branch_printf(self, body: list[Node], /) -> list[Node]:
        """Replace trailing simple printf(msg) with die(msg) in a branch body."""
        if body and self._is_simple_printf(body[-1]):
            last = body[-1]
            return [*body[:-1], Call("die", last.args, line=last.line)]
        return body

    def _transform_if_printf(self, statement: If, /) -> If:
        """Transform simple printf() at end of if-else branches into die()."""
        condition, if_body, else_body = statement.cond, statement.body, statement.else_body
        new_if = self._transform_branch_printf(if_body)
        new_else = else_body
        if else_body is not None:
            if len(else_body) == 1 and isinstance(else_body[0], If):
                transformed = self._transform_if_printf(else_body[0])
                if transformed is not else_body[0]:
                    new_else = [transformed]
            else:
                new_else = self._transform_branch_printf(else_body)
        if new_if is if_body and new_else is else_body:
            return statement
        return If(condition, new_if, new_else, line=statement.line)

    def _try_fuse_word_conditions(self, leaves: list[Node], /, *, fail_label: str, context: str) -> None:
        """Emit a flattened ``&&`` chain, fusing adjacent byte comparisons.

        Scans *leaves* for consecutive pairs where both sides are
        byte-index ``==`` comparisons on the same base variable(s) with
        adjacent indices.  Fusible pairs are emitted as a single
        word-sized comparison; non-fusible leaves fall through to the
        normal ``emit_condition`` path.

        Two fusion patterns are recognized:

        1. **byte-index vs constant pair** — ``a[N] == K1 && a[N+1] == K2``
           becomes ``cmp word [bx+N], (K2<<8)|K1`` (little-endian).

        2. **byte-index vs byte-index pair** —
           ``a[N] == b[M] && a[N+1] == b[M+1]`` becomes
           ``mov ax, [bx+N] / cmp ax, [bx+M]``.
        """
        i = 0
        while i < len(leaves):
            if i + 1 < len(leaves) and self._is_byte_eq(leaves[i]) and self._is_byte_eq(leaves[i + 1]):
                a, b = leaves[i], leaves[i + 1]
                a_left, a_right = a.left, a.right
                b_left, b_right = b.left, b.right
                # Check left-side indices are adjacent on the same base
                if self._byte_index_base_key(a_left) == self._byte_index_base_key(b_left) and b_left.index.value == a_left.index.value + 1:
                    # Pattern 1: both right sides are integer constants
                    a_lit = a_right.value if isinstance(a_right, Int) else None
                    b_lit = b_right.value if isinstance(b_right, Int) else None
                    if a_lit is not None and b_lit is not None:
                        self.validate_comparison_types(a_left, a_right)
                        operand, guarded = self._emit_byte_index_si(a_left)
                        word_mem = operand.replace("byte ", "word ")
                        word_val = (b_lit << 8) | a_lit
                        self.emit(f"        cmp {word_mem}, 0x{word_val:04x}")
                        self._si_scratch_guard_end(guarded=guarded)
                        self.emit(f"        {JUMP_WHEN_FALSE['==']} {fail_label}")
                        i += 2
                        continue
                    # Pattern 2: both right sides are byte-index with adjacent indices on same base
                    if (
                        self._is_byte_index(a_right)
                        and self._is_byte_index(b_right)
                        and self._byte_index_base_key(a_right) == self._byte_index_base_key(b_right)
                        and b_right.index.value == a_right.index.value + 1
                    ):
                        self.validate_comparison_types(a_left, a_right)
                        left_operand, left_guarded = self._emit_byte_index_si(a_left)
                        left_mem = left_operand.replace("byte ", "word ")
                        self.emit(f"        mov ax, {left_mem.removeprefix('word ')}")
                        self._si_scratch_guard_end(guarded=left_guarded)
                        right_operand, right_guarded = self._emit_byte_index_si(a_right)
                        right_mem = right_operand.replace("byte ", "word ")
                        self.emit(f"        cmp ax, {right_mem.removeprefix('word ')}")
                        self._si_scratch_guard_end(guarded=right_guarded)
                        self.emit(f"        {JUMP_WHEN_FALSE['==']} {fail_label}")
                        i += 2
                        continue
            # Not fusible — emit normally
            self.emit_condition_false_jump(condition=leaves[i], fail_label=fail_label, context=context)
            i += 1

    def _type_of_operand(self, node: Node, /) -> str:
        """Classify an operand for comparison type-checking.

        Returns one of ``"pointer"``, ``"null"``, ``"char"``, or
        ``"integer"``.  Every AST node that can legally appear inside
        a comparison must classify into one of the four buckets;
        anything else raises ``CompileError`` so no operand silently
        slips through the type check.
        """
        if isinstance(node, Char):
            return "char"
        if isinstance(node, Int):
            return "integer"
        if isinstance(node, String):
            return "pointer"
        if isinstance(node, Index):
            if self.variable_types.get(node.name) in ("char", "char*"):
                return "char"
            return "integer"
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
            message = f"undefined operand: {node.name}"
            raise CompileError(message, line=node.line)
        if isinstance(node, (BinOp, Call, LogicalAnd, LogicalOr, SizeofType, SizeofVar)):
            return "integer"
        message = f"cannot classify operand type for comparison: {type(node).__name__}"
        raise CompileError(message, line=node.line)

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
        if isinstance(last, Continue):
            return True
        if isinstance(last, Return):
            return True
        if isinstance(last, Call) and last.name in {"die", "exit"}:
            return True
        # Exhaustive if-else: both branches always exit.
        if isinstance(last, If) and last.else_body is not None:
            return CodeGenerator.always_exits(last.body) and CodeGenerator.always_exits(last.else_body)
        return False

    def ax_clear(self) -> None:
        """Clear AX tracking state."""
        self.ax_is_byte = False
        self.ax_local = None

    def builtin_asm(self, arguments: list[Node], /) -> None:
        r"""Emit an inline-asm string literal verbatim.

        Takes one string literal; C escape sequences (``\n``, ``\t``,
        ``\\``, ``\x??``) are decoded, and the result is split on
        newlines and emitted as individual lines so multi-instruction
        blocks can be written as ``asm("mov ax, 0\nmov es, ax");``.
        Pinned register values are conservatively assumed clobbered
        (see ``BUILTIN_CLOBBERS``); AX tracking is invalidated.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="asm")
        argument = arguments[0]
        if not isinstance(argument, String):
            message = "asm() argument must be a string literal"
            raise CompileError(message, line=argument.line)
        for line in _decode_string_escapes(argument.content).splitlines():
            self.emit(line)
        self.ax_clear()

    def builtin_checksum(self, arguments: list[Node], /) -> None:
        """Generate code for the checksum(buf, len) builtin.

        Computes the 1's-complement 16-bit checksum used by IP and ICMP.
        ``len`` must be even; caller is responsible for zero-padding
        odd-length buffers.  Returns the folded, complemented checksum
        in AX, ready to store in the header field.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="checksum")
        buffer_argument, length_argument = arguments
        self.emit_si_from_argument(buffer_argument)
        self.emit_register_from_argument(argument=length_argument, register="cx")
        label_index = self.new_label()
        self.emit("        cld")
        self.emit("        xor bx, bx")
        self.emit("        shr cx, 1")
        self.emit(f".ck_loop_{label_index}:")
        self.emit("        lodsw")
        self.emit("        add bx, ax")
        self.emit("        adc bx, 0")
        self.emit(f"        loop .ck_loop_{label_index}")
        self.emit("        not bx")
        self.emit("        mov ax, bx")
        self.ax_clear()

    def builtin_chmod(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the chmod() builtin.

        Returns 0 on success or an ERR_* code on failure.  When
        *fuse_exit* is True, emits ``jnc FUNCTION_EXIT`` instead of
        converting the carry flag to a 0-or-error integer.  When
        *fuse_die* is set, emits a direct ``jc FUNCTION_DIE`` with the
        given message preloaded in SI/CX.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="chmod")
        self.emit_si_from_argument(arguments[0])
        self.generate_expression(arguments[1])
        self._emit_syscall("FS_CHMOD")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_close(self, arguments: list[Node], /) -> None:
        """Generate code for the close() builtin.

        Closes a file descriptor.  ``close(fd)`` emits
        ``mov bx, <fd> / mov ah, SYS_IO_CLOSE / int 30h``.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="close")
        self.emit_register_from_argument(argument=arguments[0], register="bx")
        self._emit_syscall("IO_CLOSE")

    def builtin_datetime(self, arguments: list[Node], /) -> None:
        """Generate code for the datetime() builtin.

        Returns unsigned seconds since 1970-01-01 UTC in DX:AX. Valid
        through the year 2106 (32-bit epoch overflow).
        """
        self._check_argument_count(arguments=arguments, expected=0, name="datetime")
        self._emit_syscall("RTC_DATETIME")

    def builtin_die(self, arguments: list[Node], /) -> None:
        """Generate code for the die() builtin.

        Pre-loads SI and CX (string + length) and jumps to a shared
        ``.die`` label that calls ``write_stdout`` then exits.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="die")
        argument = arguments[0]
        if not isinstance(argument, String):
            message = "die() requires a string literal"
            raise CompileError(message, line=argument.line)
        label = self.new_string_label(argument.content)
        length = string_byte_length(argument.content)
        self.emit(f"        mov si, {label}")
        self.emit(f"        mov cx, {length}")
        self.emit("        jmp FUNCTION_DIE")

    def builtin_exec(self, arguments: list[Node], /) -> None:
        """Generate code for the exec(name) builtin.

        Emits ``mov si, <name> / mov ah, SYS_EXEC / int 30h``.  On
        success, control is transferred to the loaded program and never
        returns here.  On failure (CF set), AL contains an ``ERROR_*``
        code; ``xor ah, ah`` zero-extends it for comparison against
        ``ERROR_NOT_EXECUTE`` etc.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="exec")
        self.emit_si_from_argument(arguments[0])
        self._emit_syscall("EXEC")
        self.emit("        xor ah, ah")
        self.ax_clear()

    def builtin_exit(self, arguments: list[Node], /) -> None:
        """Generate code for the exit() builtin."""
        self._check_argument_count(arguments=arguments, expected=0, name="exit")
        self.emit("        jmp FUNCTION_EXIT")

    def builtin_fstat(self, arguments: list[Node], /) -> None:
        """Generate code for the fstat() builtin.

        ``fstat(fd)`` emits ``mov bx, <fd> / mov ah, SYS_IO_FSTAT /
        int 30h``.  Returns the file mode (flags byte) in AX.
        The syscall also returns CX:DX = file size, but those are
        discarded here.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="fstat")
        self.emit_register_from_argument(argument=arguments[0], register="bx")
        self._emit_syscall("IO_FSTAT")
        self.emit("        xor ah, ah")
        self.ax_clear()

    def builtin_getchar(self, arguments: list[Node], /) -> None:
        """Generate code for the getchar() builtin.

        Reads a single byte from stdin (blocking) via
        FUNCTION_GET_CHARACTER.  Returns the byte zero-extended in AX.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="getchar")
        self.emit("        call FUNCTION_GET_CHARACTER")
        self.emit("        xor ah, ah")
        self.ax_clear()

    def builtin_mac(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the mac(buffer) builtin.

        Reads the cached NIC MAC address (6 bytes) into ``buffer``.
        Returns 0 on success, 1 if no NIC is present.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="mac")
        self.emit_register_from_argument(argument=arguments[0], register="di")
        self._emit_syscall("NET_MAC")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=False)

    def builtin_memcpy(self, arguments: list[Node], /) -> None:
        """Generate code for the memcpy(destination, source, n) builtin.

        Emits ``mov di, <destination> / mov si, <source> / mov cx, <n>
        / cld / rep movsb``.  Byte-wise copy; caller's DI, SI, CX are
        clobbered.
        """
        self._check_argument_count(arguments=arguments, expected=3, name="memcpy")
        destination_argument, source_argument, count_argument = arguments
        self.emit_register_from_argument(argument=destination_argument, register="di")
        self.emit_register_from_argument(argument=source_argument, register="si")
        self.emit_register_from_argument(argument=count_argument, register="cx")
        self.emit("        cld")
        self.emit("        rep movsb")
        self.ax_clear()

    def builtin_mkdir(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the mkdir() builtin.

        Returns 0 on success or an ERR_* code on failure.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="mkdir")
        self.emit_si_from_argument(arguments[0])
        self._emit_syscall("FS_MKDIR")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_net_open(self, arguments: list[Node], /) -> None:
        """Generate code for the net_open(type, protocol) builtin.

        ``net_open(type, protocol)`` emits ``mov al, <type> /
        mov dl, <protocol> / mov ah, SYS_NET_OPEN / int 30h`` where type
        is SOCK_RAW (0) or SOCK_DGRAM (1) and protocol is IPPROTO_UDP (17)
        or IPPROTO_ICMP (1) for datagram sockets (ignored for raw
        Ethernet sockets — pass 0).  Returns fd in AX on success, or -1
        if no NIC is present.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="net_open")
        type_argument, protocol_argument = arguments
        if isinstance(type_argument, Int) or (isinstance(type_argument, Var) and type_argument.name in self.NAMED_CONSTANTS):
            self.emit(f"        mov al, {type_argument.value if isinstance(type_argument, Int) else type_argument.name}")
        else:
            self.generate_expression(type_argument)
        self.emit_register_from_argument(argument=protocol_argument, register="dl")
        self._emit_syscall("NET_OPEN")
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        self.emit("        mov ax, -1")
        self.emit(f".ok_{label_index}:")
        self.ax_clear()

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
            raise CompileError(message, line=arguments[0].line if arguments else None)
        name_argument = arguments[0]
        flags_argument = arguments[1]
        self.emit_si_from_argument(name_argument)
        if (flags_expr := self._constant_expression(flags_argument)) is not None:
            for name in self._collect_constant_references(flags_argument):
                self.emit_constant_reference(name)
            self.emit(f"        mov al, {flags_expr}")
        else:
            self.generate_expression(flags_argument)
        if len(arguments) == 3:
            self.emit_register_from_argument(argument=arguments[2], register="dl")
        self._emit_syscall("IO_OPEN")
        self.ax_clear()

    def builtin_parse_ip(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the parse_ip(string, buffer) builtin.

        Parses a dotted-decimal IP string into a 4-byte buffer.
        Returns 0 on success, 1 on parse error.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="parse_ip")
        self.emit_si_from_argument(arguments[0])
        self.emit_register_from_argument(argument=arguments[1], register="di")
        self.emit("        call parse_ip")
        self.required_includes.add("parse_ip.asm")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=False)

    def builtin_print_datetime(self, arguments: list[Node], /) -> None:
        """Generate code for the print_datetime(unsigned long) builtin.

        Prints the epoch value as ``YYYY-MM-DD HH:MM:SS`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_datetime")
        self.generate_long_expression(arguments[0])
        self.emit("        call FUNCTION_PRINT_DATETIME")

    def builtin_print_ip(self, arguments: list[Node], /) -> None:
        """Generate code for the print_ip(buffer) builtin.

        Prints a 4-byte IP address as ``A.B.C.D`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_ip")
        self.emit_si_from_argument(arguments[0])
        self.emit("        call FUNCTION_PRINT_IP")

    def builtin_print_mac(self, arguments: list[Node], /) -> None:
        """Generate code for the print_mac(buffer) builtin.

        Prints a 6-byte MAC address as ``XX:XX:XX:XX:XX:XX`` (no newline).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="print_mac")
        self.emit_si_from_argument(arguments[0])
        self.emit("        call FUNCTION_PRINT_MAC")

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
            raise CompileError(message, line=arguments[0].line if arguments else None)
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
            raise CompileError(message, line=arguments[0].line)
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

    def builtin_putchar(self, arguments: list[Node], /) -> None:
        """Generate code for the putchar() builtin."""
        self._check_argument_count(arguments=arguments, expected=1, name="putchar")
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
        self._check_argument_count(arguments=arguments, expected=3, name="read")
        fd_argument, buffer_argument, count_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self.emit_register_from_argument(argument=buffer_argument, register="di")
        self.emit_register_from_argument(argument=count_argument, register="cx")
        self._emit_syscall("IO_READ")
        self.ax_clear()

    def builtin_reboot(self, arguments: list[Node], /) -> None:
        """Generate code for the reboot() builtin.

        Emits ``mov ah, SYS_REBOOT / int 30h``.  Does not return on
        success; the kernel triggers a warm reboot via the keyboard
        controller.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="reboot")
        self._emit_syscall("REBOOT")

    def builtin_recvfrom(self, arguments: list[Node], /) -> None:
        """Generate code for the recvfrom() builtin.

        ``recvfrom(fd, buf, len, port)`` emits ``mov bx, <fd> /
        mov di, <buf> / mov cx, <len> / mov dx, <port> /
        mov ah, SYS_NET_RECVFROM / int 30h``.
        Returns bytes received in AX (0 if no matching packet).
        """
        self._check_argument_count(arguments=arguments, expected=4, name="recvfrom")
        fd_argument, buffer_argument, len_argument, port_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self.emit_register_from_argument(argument=buffer_argument, register="di")
        self.emit_register_from_argument(argument=len_argument, register="cx")
        self.emit_register_from_argument(argument=port_argument, register="dx")
        self._emit_syscall("NET_RECVFROM")
        self.ax_clear()

    def builtin_rename(
        self,
        arguments: list[Node],
        /,
        *,
        fuse_die: tuple[str, int] | None = None,
        fuse_exit: bool = False,
    ) -> None:
        """Generate code for the rename() builtin.

        ``rename(oldname, newname)`` emits ``mov si, <oldname> /
        mov di, <newname> / mov ah, SYS_FS_RENAME / int 30h``.
        Returns 0 on success or an ERROR_* code on failure.
        """
        self._check_argument_count(arguments=arguments, expected=2, name="rename")
        self.emit_si_from_argument(arguments[0])
        self.emit_register_from_argument(argument=arguments[1], register="di")
        self._emit_syscall("FS_RENAME")
        self.emit_error_syscall_tail(fuse_die=fuse_die, fuse_exit=fuse_exit, preserve_al=True)

    def builtin_sendto(self, arguments: list[Node], /) -> None:
        """Generate code for the sendto() builtin.

        ``sendto(fd, buf, len, ip_ptr, src_port, dst_port)`` emits
        register setup and ``mov ah, SYS_NET_SENDTO / int 30h``.
        The 6th argument (dst_port) goes in BP (saved/restored).
        Returns bytes sent in AX, or -1 on error.
        """
        self._check_argument_count(arguments=arguments, expected=6, name="sendto")
        fd_argument, buf_argument, len_argument, ip_argument, sport_argument, dport_argument = arguments
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self.emit_si_from_argument(buf_argument)
        self.emit_register_from_argument(argument=len_argument, register="cx")
        self.emit_register_from_argument(argument=ip_argument, register="di")
        self.emit_register_from_argument(argument=sport_argument, register="dx")
        self.emit("        push bp")
        if isinstance(dport_argument, Int):
            self.emit(f"        mov bp, {dport_argument.value}")
        elif isinstance(dport_argument, Var) and dport_argument.name in self.NAMED_CONSTANTS:
            self.emit(f"        mov bp, {dport_argument.name}")
        elif isinstance(dport_argument, Var) and dport_argument.name in self.pinned_register:
            self.emit(f"        mov bp, {self.pinned_register[dport_argument.name]}")
        elif isinstance(dport_argument, Var) and self._is_memory_scalar(dport_argument.name):
            self.emit(f"        mov bp, [{self._local_address(dport_argument.name)}]")
        else:
            self.generate_expression(dport_argument)
            self.emit("        mov bp, ax")
        self._emit_syscall("NET_SENDTO")
        self.emit("        pop bp")
        # Normalize the CF error signal into AX = -1 so callers can
        # check the return value with ``< 0``.
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        self.emit("        mov ax, -1")
        self.emit(f".ok_{label_index}:")
        self.ax_clear()

    def builtin_set_exec_arg(self, arguments: list[Node], /) -> None:
        """Generate code for the set_exec_arg(arg) builtin.

        Writes the 16-bit pointer *arg* to ``[EXEC_ARG]`` so that
        ``FUNCTION_PARSE_ARGV`` in the next exec()'d program can find
        it.  Pass NULL (0) to clear.  Used by the shell to forward
        command arguments into child programs.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="set_exec_arg")
        self.generate_expression(arguments[0])
        self.emit("        mov [EXEC_ARG], ax")

    def builtin_shutdown(self, arguments: list[Node], /) -> None:
        """Generate code for the shutdown() builtin.

        Emits ``mov ah, SYS_SHUTDOWN / int 30h``.  Does not return on
        success.  On APM failure the syscall returns, letting the caller
        print a diagnostic and continue.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="shutdown")
        self._emit_syscall("SHUTDOWN")

    def builtin_sleep(self, arguments: list[Node], /) -> None:
        """Generate code for the sleep(milliseconds) builtin.

        ``sleep(ms)`` emits ``mov cx, <ms> / mov ah, SYS_RTC_SLEEP /
        int 30h``.  Busy-waits for the requested duration.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="sleep")
        self.emit_register_from_argument(argument=arguments[0], register="cx")
        self._emit_syscall("RTC_SLEEP")

    def builtin_strlen(self, arguments: list[Node], /) -> None:
        """Generate code for the strlen() builtin.

        ``strlen(ptr)`` scans for a null terminator and returns the
        string length in AX.  Uses ``repne scasb`` (clobbers CX, DI).
        """
        self._check_argument_count(arguments=arguments, expected=1, name="strlen")
        self.emit_register_from_argument(argument=arguments[0], register="di")
        self.emit("        xor al, al")
        self.emit("        mov cx, 0FFFFh")
        self.emit("        cld")
        self.emit("        repne scasb")
        self.emit("        mov ax, 0FFFEh")
        self.emit("        sub ax, cx")
        self.ax_clear()

    def builtin_ticks(self, arguments: list[Node], /) -> None:
        """Generate code for the ticks() builtin.

        Returns the low 16 bits of the BIOS timer tick counter
        (~18.2 Hz).  Suitable for measuring short elapsed intervals —
        subtract two readings to get a tick count in [0, 65535].  The
        counter wraps roughly once an hour.
        """
        self._check_argument_count(arguments=arguments, expected=0, name="ticks")
        self.emit("        xor ah, ah")
        self.emit("        int 1Ah")
        self.emit("        mov ax, dx")
        self.ax_clear()

    def builtin_uptime(self, arguments: list[Node], /) -> None:
        """Generate code for the uptime() builtin."""
        self._check_argument_count(arguments=arguments, expected=0, name="uptime")
        self._emit_syscall("RTC_UPTIME")

    def builtin_video_mode(self, arguments: list[Node], /) -> None:
        """Generate code for the video_mode(mode) builtin.

        Invokes SYS_VIDEO_MODE to switch video mode; also clears the
        screen and serial terminal.  AL = mode.
        """
        self._check_argument_count(arguments=arguments, expected=1, name="video_mode")
        self.emit_register_from_argument(argument=arguments[0], register="ax")
        self._emit_syscall("VIDEO_MODE")
        self.ax_clear()

    def builtin_write(self, arguments: list[Node], /) -> None:
        """Generate code for the write() builtin.

        ``write(fd, buffer, count)`` emits ``mov bx, <fd> /
        mov si, <buffer> / mov cx, <count> / mov ah, SYS_IO_WRITE /
        int 30h``.  Returns bytes written in AX (-1 on error).
        """
        self._check_argument_count(arguments=arguments, expected=3, name="write")
        fd_argument, buffer_argument, count_argument = arguments
        self.emit_register_from_argument(argument=buffer_argument, register="si")
        self.emit_register_from_argument(argument=count_argument, register="cx")
        self.emit_register_from_argument(argument=fd_argument, register="bx")
        self._emit_syscall("IO_WRITE")
        self.ax_clear()

    def can_auto_pin(self, *, following_statement: Node | None, statement: VarDecl) -> bool:
        """Decide whether *statement* should be auto-pinned to a register."""
        if len(self.pinned_register) >= len(self.safe_pin_registers):
            return False
        init = statement.init
        if init is None:
            return True
        # Call initializers stay in memory so they can participate in
        # error-return fusion without clobbering a pin.
        return not isinstance(init, Call)

    def compute_safe_pin_registers(self, body: list[Node], /) -> tuple[str, ...]:
        """Return the pinnable register pool ordered by clobber cost.

        All registers in the pool are pinnable; :meth:`generate_call`
        wraps each call with ``push``/``pop`` for any caller pin the
        callee clobbers.  Ordering by clobber count so that the first
        (most-referenced) candidate lands on the cheapest register.
        The per-function counts are memoised on
        :attr:`register_clobber_counts` so the cost model in
        :meth:`_select_auto_pin_candidates` can reuse them.

        ``main`` (recognised by :attr:`elide_frame`) extends the base
        pool with BP — it doesn't need BP as a frame pointer and
        every callee preserves BP across calls (builtins via the
        kernel's ``pusha``/``popa`` syscall wrapper, user functions
        via the standard ``push bp`` / ``pop bp`` prologue).  That
        gives main a fifth register at zero clobber cost, perfect
        for a high-traffic flag (``dirty``) or scroll counter
        (``view_line``).

        Subscript codegen uses SI as its scratch register; SI isn't
        in the pool so no extra exclusion is needed for subscript
        presence.
        """
        pool = (*self.REGISTER_POOL, "bp") if self.elide_frame else self.REGISTER_POOL
        clobber_counts: dict[str, int] = dict.fromkeys(pool, 0)

        def visit(node: Node) -> None:
            if isinstance(node, Call):
                if node.name in self.user_functions:
                    # User functions follow the standard cdecl prologue
                    # (``push bp / mov bp, sp / … / pop bp``) which
                    # preserves the caller's BP, so BP is omitted from
                    # the user-call clobber set even when it's pinned.
                    for register in self.REGISTER_POOL:
                        clobber_counts[register] += 1
                else:
                    if node.name not in self.BUILTIN_CLOBBERS:
                        message = f"unknown function: {node.name}"
                        raise CompileError(message, line=node.line)
                    for register in self.BUILTIN_CLOBBERS[node.name]:
                        if register in clobber_counts:
                            clobber_counts[register] += 1
            for slot in getattr(type(node), "__slots__", ()):
                child = getattr(node, slot, None)
                if isinstance(child, Node):
                    visit(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, Node):
                            visit(item)

        for statement in body:
            visit(statement)
        self.register_clobber_counts = clobber_counts
        pool_index = {register: index for index, register in enumerate(pool)}
        # Sort by clobber count, then declaration order — but force BP
        # to the tail of the list.  BP can't be used as an index
        # register for DS-relative addressing in real mode (every
        # ``buffer[bp_var]`` access pays a 2-byte ``mov si, bp``), so
        # the highest-traffic candidate (which usually IS an index
        # base) should land on a BX/DI/CX/DX slot first.  BP picks up
        # whatever lower-priority scalar candidate is left over —
        # zero-clobber across every callee makes it pure profit
        # there.
        return tuple(sorted(pool, key=lambda register: (clobber_counts[register], pool_index[register])))

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
            other_statements = statements[:index] + statements[index + 2 :]
            name = statement.name
            if any(CodeGenerator._statement_references(other, name) for other in other_statements):
                continue
            self.virtual_long_locals.add(name)

    def emit(self, line: str = "") -> None:
        """Append a line of assembly to the output buffer."""
        self.lines.append(line)

    def emit_argument_vector_startup(self, parameters: list[Param], /, *, body: list[Node]) -> list[Node]:
        """Emit inline startup code that parses EXEC_ARG into argc/argv.

        Registers ``argv`` as a constant alias to ``ARGV`` so all
        subsequent accesses use the kernel constant directly, avoiding
        a memory local and store/reload traffic.

        When the first statement in *body* is ``if (argc != N) die(msg)``,
        the argc check is fused directly into the startup using
        ``cmp cx, N`` (before CX is clobbered), eliminating the
        ``_l_argc`` memory local entirely.  Returns the (possibly
        trimmed) body.
        """
        argc_name = None
        argv_name = None
        for param in parameters:
            if param.is_array:
                argv_name = param.name
            elif argc_name is None:
                argc_name = param.name
        if not argv_name:
            return body

        # argv is always the fixed ARGV address — register as constant alias.
        self.constant_aliases[argv_name] = "ARGV"

        self.emit("        cld")
        self.emit("        mov di, ARGV")
        self.emit("        call FUNCTION_PARSE_ARGV")

        # Try to fuse the first body statement: if (argc != N) die(msg)
        fused_argc = False
        if argc_name and body:
            first = body[0]
            if (
                isinstance(first, If)
                and first.else_body is None
                and len(first.body) == 1
                and isinstance(first.body[0], Call)
                and first.body[0].name == "die"
                and len(first.body[0].args) == 1
                and isinstance(first.body[0].args[0], String)
                and isinstance(first.cond, BinOp)
                and first.cond.op == "!="
                and isinstance(first.cond.left, Var)
                and first.cond.left.name == argc_name
                and isinstance(first.cond.right, Int)
            ):
                die_message = first.body[0].args[0]
                die_label = self.new_string_label(die_message.content)
                die_length = string_byte_length(die_message.content)
                expected = first.cond.right.value
                self.emit(f"        cmp cx, {expected}")
                self.emit(f"        mov si, {die_label}")
                self.emit(f"        mov cx, {die_length}")
                self.emit("        jne FUNCTION_DIE")
                fused_argc = True
                body = body[1:]

        if argc_name and not fused_argc:
            self.emit(f"        mov [{self._local_address(argc_name)}], cx")
        return body

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
            source_register = self.pinned_register[right.name]
            if source_register != "cx":
                self.emit(f"        mov cx, {source_register}")
        elif isinstance(right, Var) and self._is_memory_scalar(right.name):
            self.generate_expression(left)
            self.emit(f"        mov cx, [{self._local_address(right.name)}]")
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
        the load into AX.  ``NULL`` and other named constants are
        treated as constant immediates.
        """
        literal = None
        is_zero = False
        if isinstance(right, Int):
            literal = str(right.value)
            is_zero = right.value == 0
        elif isinstance(right, Var) and right.name in self.NAMED_CONSTANTS:
            literal = right.name
            is_zero = right.name == "NULL"
        if literal is not None:
            if isinstance(left, Var) and left.name in self.pinned_register:
                register = self.pinned_register[left.name]
                if is_zero:
                    self.emit(f"        test {register}, {register}")
                else:
                    self.emit(f"        cmp {register}, {literal}")
                return
            # Memory-backed local compared to a constant: fuse into a
            # direct ``cmp word [L], imm`` so we skip the ``mov ax, [L]``
            # load.  Safe because the flags are consumed by the next
            # conditional jump and AX's prior value was not promised.
            if (
                isinstance(left, Var)
                and self._is_memory_scalar(left.name)
                and left.name not in self.variable_arrays
                and left.name != self.ax_local
                and self.variable_types.get(left.name) != "unsigned long"
            ):
                address = self._local_address(left.name)
                if is_zero:
                    self.emit(f"        cmp word [{address}], 0")
                else:
                    self.emit(f"        cmp word [{address}], {literal}")
                return
            # Byte-indexed variable compared to a constant: fuse into
            # ``cmp byte [bx+N], imm`` so we skip the load-into-AL and
            # the zero-extend into AX.
            if self._is_byte_index(left):
                operand, guarded = self._emit_byte_index_si(left)
                if is_zero:
                    self.emit(f"        cmp {operand}, 0")
                else:
                    self.emit(f"        cmp {operand}, {literal}")
                self._si_scratch_guard_end(guarded=guarded)
                return
            self.generate_expression(left)
            if is_zero:
                self.emit("        test al, al" if self.ax_is_byte else "        test ax, ax")
            else:
                register = "al" if self.ax_is_byte else "ax"
                self.emit(f"        cmp {register}, {literal}")
        else:
            # Two byte-indexed variables: load left byte into AL, then
            # compare directly against the right byte in memory.  Saves
            # the zero-extend, push/pop, and CX round-trip.
            if self._is_byte_index(left) and self._is_byte_index(right):
                left_operand, left_guarded = self._emit_byte_index_si(left)
                left_mem = left_operand.removeprefix("byte ")
                self.emit(f"        mov al, {left_mem}")
                self._si_scratch_guard_end(guarded=left_guarded)
                right_operand, right_guarded = self._emit_byte_index_si(right)
                right_mem = right_operand.removeprefix("byte ")
                self.emit(f"        cmp al, {right_mem}")
                self._si_scratch_guard_end(guarded=right_guarded)
                return
            # Fast path: right is a pinned register variable.  Compare
            # AX against it directly, skipping the CX load and any
            # push/pop protection.  When the pinned register is CX we
            # additionally require ``left`` to be a leaf expression so
            # generate_expression can't clobber CX mid-compare.
            if isinstance(right, Var) and right.name in self.pinned_register:
                source = self.pinned_register[right.name]
                if source != "cx" or isinstance(left, (Int, Var, String)):
                    self.generate_expression(left)
                    self.emit(f"        cmp ax, {source}")
                    return
            # Fast path: right is a memory-backed local.  ``cmp ax, [mem]``
            # skips the CX load entirely.
            if (
                isinstance(right, Var)
                and self._is_memory_scalar(right.name)
                and right.name not in self.pinned_register
                and right.name not in self.variable_arrays
                and self.variable_types.get(right.name) != "unsigned long"
            ):
                self.generate_expression(left)
                self.emit(f"        cmp ax, [{self._local_address(right.name)}]")
                return
            # emit_binary_operator_operands clobbers CX; save it when a
            # pinned variable lives there (push/pop don't modify flags,
            # so the cmp's flags survive the restore for the caller's
            # conditional jump).
            cx_pinned = any(register == "cx" for register in self.pinned_register.values())
            if cx_pinned:
                self.emit("        push cx")
            self.emit_binary_operator_operands(left, right)
            self.emit("        cmp ax, cx")
            if cx_pinned:
                self.emit("        pop cx")

    def emit_condition(self, *, condition: Node, context: str) -> str:
        """Validate a condition, emit a comparison, and return the operator.

        ``carry_return`` call conditions — ``if (foo())`` / ``while
        (foo())`` / ``if (foo() == 0)`` where ``foo`` is declared with
        ``__attribute__((carry_return))`` — skip the ``cmp`` path
        entirely: the ``call`` itself leaves CF holding the truth
        value, and the caller dispatches through ``jc`` / ``jnc`` via
        the synthetic ``"carry"`` / ``"not_carry"`` operators.
        ``parse_condition`` wraps a bare expression as ``expr != 0``,
        so the detected shape is always ``BinOp('!=' | '==', Call,
        Int(0))``.

        Raises:
            CompileError: If the condition is not a comparison.

        """
        if (
            isinstance(condition, BinOp)
            and condition.op in ("!=", "==")
            and isinstance(condition.right, Int)
            and condition.right.value == 0
            and isinstance(condition.left, Call)
            and condition.left.name in self.carry_return_functions
        ):
            self.generate_call(condition.left, discard_return=True)
            return "carry" if condition.op == "!=" else "not_carry"
        if not isinstance(condition, BinOp) or condition.op not in JUMP_WHEN_FALSE:
            message = f"{context} condition must be a comparison, got {condition}"
            raise CompileError(message, line=condition.line)
        self.validate_comparison_types(condition.left, condition.right)
        self.emit_comparison(condition.left, condition.right)
        return condition.op

    def emit_condition_false_jump(self, *, condition: Node, fail_label: str, context: str) -> None:
        """Emit a condition that jumps to ``fail_label`` when false.

        For ``&&``, short-circuits by recursing on each operand with
        the same fail label — any false leg jumps directly to the
        failure target.  For ``||``, jumps past the right leg as soon
        as the left leg is true, otherwise re-enters the false-jump on
        the right leg.

        When the ``&&`` chain contains adjacent byte-index ``==``
        comparisons on the same base, they are fused into word-sized
        comparisons (see :meth:`_try_fuse_word_conditions`).
        """
        if isinstance(condition, LogicalAnd):
            leaves = self._flatten_and(condition)
            self._try_fuse_word_conditions(leaves, fail_label=fail_label, context=context)
            return
        if isinstance(condition, LogicalOr):
            pass_label = f".lor_{self.new_label()}"
            self.emit_condition_true_jump(condition=condition.left, success_label=pass_label, context=context)
            self.emit_condition_false_jump(condition=condition.right, fail_label=fail_label, context=context)
            self.emit(f"{pass_label}:")
            return
        operator = self.emit_condition(condition=condition, context=context)
        self.emit(f"        {JUMP_WHEN_FALSE[operator]} {fail_label}")

    def emit_condition_true_jump(self, *, condition: Node, success_label: str, context: str) -> None:
        """Emit a condition that jumps to ``success_label`` when true.

        Dual of :meth:`emit_condition_false_jump`; used for the ``||``
        short-circuit so that a truthy left leg can skip the right.
        """
        if isinstance(condition, LogicalOr):
            self.emit_condition_true_jump(condition=condition.left, success_label=success_label, context=context)
            self.emit_condition_true_jump(condition=condition.right, success_label=success_label, context=context)
            return
        if isinstance(condition, LogicalAnd):
            skip_label = f".land_{self.new_label()}"
            self.emit_condition_false_jump(condition=condition.left, fail_label=skip_label, context=context)
            self.emit_condition_true_jump(condition=condition.right, success_label=success_label, context=context)
            self.emit(f"{skip_label}:")
            return
        operator = self.emit_condition(condition=condition, context=context)
        self.emit(f"        {JUMP_WHEN_TRUE[operator]} {success_label}")

    def emit_constant_reference(self, name: str) -> None:
        """Record a reference to a NAMED_CONSTANT.

        If the constant requires an extra NASM %include to provide its
        symbol (see :attr:`NAMED_CONSTANT_INCLUDES`), queue the include
        for emission at output time.
        """
        include = self.NAMED_CONSTANT_INCLUDES.get(name)
        if include is not None:
            self.required_includes.add(include)

    def emit_error_syscall_tail(
        self,
        *,
        fuse_die: tuple[str, int] | None,
        fuse_exit: bool,
        preserve_al: bool,
    ) -> None:
        """Emit the shared tail for an error-returning syscall.

        - ``fuse_die=(label, length)`` → preload SI/CX and
          ``jc FUNCTION_DIE`` so the if-error-die block disappears.
        - ``fuse_exit`` → ``jnc FUNCTION_EXIT`` (for
          ``if (!err) return;`` fusion).
        - Otherwise, convert the carry flag into a 0-or-error integer
          in AX.  ``preserve_al`` keeps AL on the error path (syscalls
          that return an ERROR_* code); False hard-codes 1.
        """
        if fuse_die is not None:
            die_label, die_length = fuse_die
            self.emit(f"        mov si, {die_label}")
            self.emit(f"        mov cx, {die_length}")
            self.emit("        jc FUNCTION_DIE")
            return
        if fuse_exit:
            self.emit("        jnc FUNCTION_EXIT")
            return
        label_index = self.new_label()
        self.emit(f"        jnc .ok_{label_index}")
        if preserve_al:
            self.emit("        xor ah, ah")
        else:
            self.emit("        mov ax, 1")
        self.emit(f"        jmp .done_{label_index}")
        self.emit(f".ok_{label_index}:")
        self.emit("        xor ax, ax")
        self.emit(f".done_{label_index}:")

    def emit_register_from_argument(self, *, argument: Node, register: str) -> None:
        """Load an argument into a specific 16-bit register.

        Handles pinned variables, memory locals, named constants,
        integer literals, and general expressions (evaluated via AX).
        """
        if isinstance(argument, Int):
            self.emit(f"        mov {register}, {argument.value}")
        elif isinstance(argument, Var) and argument.name in self.NAMED_CONSTANTS:
            self.emit_constant_reference(argument.name)
            self.emit(f"        mov {register}, {argument.name}")
        elif isinstance(argument, Var) and argument.name in self.constant_aliases:
            self.emit(f"        mov {register}, {self.constant_aliases[argument.name]}")
        elif isinstance(argument, Var) and argument.name in self.pinned_register:
            source = self.pinned_register[argument.name]
            if source != register:
                self.emit(f"        mov {register}, {source}")
        elif isinstance(argument, Var) and argument.name in self.register_aliased_globals:
            source = self.register_aliased_globals[argument.name]
            if source != register:
                self.emit(f"        mov {register}, {source}")
        elif isinstance(argument, Var) and argument.name == self.ax_local:
            if register != "ax":
                self.emit(f"        mov {register}, ax")
        elif isinstance(argument, Var) and argument.name in self.global_arrays:
            self.emit(f"        mov {register}, _g_{argument.name}")
        elif isinstance(argument, Var) and self._is_memory_scalar(argument.name):
            self.emit(f"        mov {register}, [{self._local_address(argument.name)}]")
        elif isinstance(argument, String):
            self.emit(f"        mov {register}, {self.new_string_label(argument.content)}")
        elif (constant_expr := self._constant_expression(argument)) is not None:
            for name in self._collect_constant_references(argument):
                self.emit_constant_reference(name)
            self.emit(f"        mov {register}, {constant_expr}")
        else:
            self.generate_expression(argument)
            if register != "ax":
                self.emit(f"        mov {register}, ax")

    def emit_si_from_argument(self, argument: Node, /) -> None:
        """Load a string or expression argument into SI."""
        if isinstance(argument, String):
            self.emit(f"        mov si, {self.new_string_label(argument.content)}")
        elif isinstance(argument, Var) and argument.name in self.constant_aliases:
            self.emit(f"        mov si, {self.constant_aliases[argument.name]}")
        elif isinstance(argument, Var) and argument.name in self.global_arrays:
            self.emit(f"        mov si, _g_{argument.name}")
        elif (constant_expr := self._constant_expression(argument)) is not None:
            for name in self._collect_constant_references(argument):
                self.emit_constant_reference(name)
            self.emit(f"        mov si, {constant_expr}")
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
        if name in self.global_arrays:
            message = f"cannot assign to array '{name}'"
            raise CompileError(message)
        if self.variable_types.get(name) == "unsigned long":
            self.ax_clear()
            self.generate_long_expression(expression)
            if name in self.virtual_long_locals:
                self.live_long_local = name
                return
            address = self._local_address(name)
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
        direct_register: str | None = None
        if name in self.pinned_register:
            direct_register = self.pinned_register[name]
        elif name in self.register_aliased_globals:
            direct_register = self.register_aliased_globals[name]
        if direct_register is not None:
            if isinstance(expression, Int):
                if expression.value == 0:
                    self.emit(f"        xor {direct_register}, {direct_register}")
                else:
                    self.emit(f"        mov {direct_register}, {expression.value}")
                return
            if isinstance(expression, String):
                label = self.new_string_label(expression.content)
                self.emit(f"        mov {direct_register}, {label}")
                return
            if isinstance(expression, Var) and expression.name in self.NAMED_CONSTANTS:
                self.emit(f"        mov {direct_register}, {expression.name}")
                return
            if isinstance(expression, Var) and expression.name in self.global_arrays:
                self.emit(f"        mov {direct_register}, _g_{expression.name}")
                return
        # Tell nested expression handling that the pinned destination
        # register (if any) will be overwritten at end of this store, so
        # they don't need to push/pop it to preserve the old value.
        previous_store_target = self.store_target_register
        self.store_target_register = direct_register
        self.generate_expression(expression)
        self.store_target_register = previous_store_target
        if direct_register is not None:
            if direct_register != "ax":
                self.emit(f"        mov {direct_register}, ax")
        else:
            self.emit(f"        mov [{self._local_address(name)}], ax")
        self.ax_is_byte = False
        self.ax_local = name
        # ``mov ax, D / <op> ax, ... / mov D, ax`` sequences are fused
        # by the late peephole passes into a single ``<op> D, ...`` (or
        # into a compute-into-pinned-register form), neither of which
        # leaves AX holding the new value.  When that fusion applies,
        # the ``ax_local`` tracking we just set would let a downstream
        # read of ``name`` skip its reload and pick up the pre-sequence
        # AX contents instead.  Invalidate the tracking here so the
        # reload happens naturally.
        if self._peephole_will_strand_ax():
            self.ax_local = None

    def fuse_trailing_printf(self, body: list[Node], /) -> list[Node]:
        """Transform trailing simple printf() calls into die() for main.

        Handles both a direct trailing ``printf(msg)`` and ``printf(msg)``
        at the end of branches in a trailing if-else chain.
        """
        if not body:
            return body
        last = body[-1]
        if self._is_simple_printf(last):
            return [*body[:-1], Call("die", last.args)]
        if isinstance(last, If):
            transformed = self._transform_if_printf(last)
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
        if self.defines:
            self.emit()
            for name in sorted(self.defines):
                self.emit(f"%define {name} {self.defines[name]}")
        self.emit()
        for function in ast.functions:
            if function.name != "main":
                self.user_functions[function.name] = len(function.params)
                if function.regparm_count > 0:
                    self.fastcall_functions.add(function.name)
                if function.carry_return:
                    self.carry_return_functions.add(function.name)
                if function.always_inline:
                    self._register_inline_body(function)
        self._register_globals(ast.globals)
        self._analyze_user_function_conventions(ast.functions)
        # Emit main first so execution starts at PROGRAM_BASE.
        main_func = None
        helpers: list[Node] = []
        for function in ast.functions:
            if function.name == "main":
                main_func = function
            else:
                helpers.append(function)
        if main_func is not None:
            self.generate_function(main_func)
        for function in helpers:
            self.generate_function(function)
        self.peephole()
        for include in sorted(self.required_includes):
            self.emit(f'%include "{include}"')
        # File-scope ``asm("...")`` blocks are emitted BEFORE globals /
        # strings / array data.  When the block holds code (for example
        # the assembler in src/c/asm.c), this keeps the mutable global-
        # variable section away from the same 4K page as frequently-
        # executed instructions — QEMU's TCG invalidates per page on
        # stores, and mixing the two caused a 2x runtime slowdown on
        # the self-hosted assembler's pass loop.
        file_scope_asm = [decl for decl in ast.globals if isinstance(decl, InlineAsm)]
        if file_scope_asm:
            self.emit(";; --- inline asm ---")
            for decl in file_scope_asm:
                for line in _decode_string_escapes(decl.content).splitlines():
                    self.emit(line)
        self._emit_global_storage()
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
        # Sentinel label at the very end so inline asm can address the
        # first byte past the loaded image (scratch buffers, heap bases,
        # etc.).  Zero bytes, so it does not affect programs that ignore
        # it.
        self.emit("_program_end:")
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
            next_is_exit = i + 1 < len(statements) and (statements[i + 1] == Call("exit", []) or isinstance(statements[i + 1], Return))
            if self._is_simple_printf(statement) and next_is_exit:
                self.builtin_die(statement.args)
                i += 2
                continue
            # Fuse die-on-error syscall + if-(non)zero-die.
            if isinstance(statement, VarDecl):
                init = statement.init
            elif isinstance(statement, Assign) and isinstance(statement.expr, Call):
                init = statement.expr
            else:
                init = None
            # Fuse `if (cond) { die(msg); }` into pre-load SI+CX + jCC .die.
            # AX tracking is preserved because the die path doesn't fall
            # through — the continuation path sees AX unchanged from before.
            if isinstance(statement, If) and statement.else_body is None and len(statement.body) == 1:
                inner = statement.body[0]
                if (
                    isinstance(inner, Call)
                    and inner.name == "die"
                    and isinstance(statement.cond, BinOp)
                    and statement.cond.op in JUMP_WHEN_FALSE
                ):
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
            # Fuse error-returning syscall + if-truthy-die:
            #     int err = syscall(...);
            #     if (err) { die(msg); }
            # Emit the syscall, then preload SI/CX with the die
            # message and a single `jc FUNCTION_DIE` — no memory
            # round-trip for err, no CF->integer normalization.  Only
            # fires when `err` is never read after the if.
            if init is not None and isinstance(init, Call) and init.name in self.ERROR_RETURNING_BUILTINS and i + 1 < len(statements):
                next_stmt = statements[i + 1]
                die_call = None
                # Match cond: `err` (BinOp != 0) or `!err` (BinOp == 0)
                cond = next_stmt.cond if isinstance(next_stmt, If) else None
                is_truthy_cond = (
                    isinstance(cond, BinOp)
                    and cond.op == "!="
                    and isinstance(cond.left, Var)
                    and cond.left.name == statement.name
                    and cond.right == Int(0)
                )
                if (
                    is_truthy_cond
                    and next_stmt.else_body is None
                    and len(next_stmt.body) == 1
                    and isinstance(next_stmt.body[0], Call)
                    and next_stmt.body[0].name == "die"
                    and len(next_stmt.body[0].args) == 1
                    and isinstance(next_stmt.body[0].args[0], String)
                ):
                    die_call = next_stmt.body[0]
                if die_call is not None and not self._is_live_after(name=statement.name, statements=statements[i + 2 :]):
                    die_message = die_call.args[0]
                    die_label = self.new_string_label(die_message.content)
                    die_length = string_byte_length(die_message.content)
                    self.visible_vars.add(statement.name)
                    handler = getattr(self, f"builtin_{init.name}")
                    handler(init.args, fuse_die=(die_label, die_length))
                    self.ax_clear()
                    i += 2
                    continue
            # Fuse error-returning syscall + if-zero-exit:
            #     int err = syscall(...);
            #     if (err == 0) return;
            # becomes a single `jnc FUNCTION_EXIT` after the syscall,
            # leaving AL = error code on the CF=1 fall-through so any
            # subsequent `if (err == N)` chain reads the right byte.
            if init is not None and isinstance(init, Call) and init.name in self.ERROR_RETURNING_BUILTINS and i + 1 < len(statements):
                next_stmt = statements[i + 1]
                if self._is_zero_exit_if(next_stmt):
                    self.visible_vars.add(statement.name)
                    handler = getattr(self, f"builtin_{init.name}")
                    handler(init.args, fuse_exit=True)
                    self.ax_is_byte = True
                    self.ax_local = statement.name
                    i += 2
                    continue
            self.generate_statement(statement)
            i += 1
        if saved is not None:
            self.visible_vars = saved

    def generate_call(self, statement: Call, /, *, discard_return: bool = False) -> None:
        """Generate code for a function call statement.

        When *discard_return* is True (the call is at statement level
        with its return value unused) and three or more pinned
        registers need preserving, swaps the per-register
        ``push``/``pop`` pair for a single byte ``pusha``/``popa`` —
        2 bytes instead of 2 * N.  Pusha/popa restores AX too, so the
        return value would be lost; only the discard case can take
        this shortcut.

        Raises:
            CompileError: If the called function is not a known builtin
                or user-defined function.

        """
        name = statement.name
        arguments = statement.args
        if name in self.user_functions:
            expected = self.user_functions[name]
            if len(arguments) != expected:
                message = f"{name}() expects exactly {expected} argument{'s' if expected != 1 else ''}"
                raise CompileError(message, line=statement.line)
            clobbers: frozenset[str] = frozenset(self.REGISTER_POOL)
            saved = self._pinned_registers_to_save(clobbers)
            use_pusha = discard_return and len(saved) >= 3
            if use_pusha:
                self.emit("        pusha")
            else:
                for register in saved:
                    self.emit(f"        push {register}")
            callee_pins = self.user_function_pin_params.get(name, {}) if name in self.register_convention_functions else {}
            is_fastcall = name in self.fastcall_functions
            fastcall_ax_arg: Node | None = None
            register_args: list[tuple[str, Node]] = []
            stack_args: list[Node] = []
            for index, arg in enumerate(arguments):
                if is_fastcall and index == 0:
                    fastcall_ax_arg = arg
                elif index in callee_pins:
                    register_args.append((callee_pins[index], arg))
                else:
                    stack_args.append(arg)
            # Push stack-bound arguments right-to-left (C convention).
            for arg in reversed(stack_args):
                self._emit_push_arg(arg)
            # Load register-bound arguments with topological ordering.
            self._emit_register_arg_moves(register_args)
            # Fastcall arg 0 is loaded last so earlier arg evaluation can't
            # trash AX while we're assembling the other parameters.
            if fastcall_ax_arg is not None:
                self.emit_register_from_argument(argument=fastcall_ax_arg, register="ax")
            if name in self.inline_bodies:
                self._emit_inline_body(name)
            else:
                self.emit(f"        call {name}")
            if stack_args:
                self.emit(f"        add sp, {len(stack_args) * 2}")
            if use_pusha:
                self.emit("        popa")
            else:
                for register in reversed(saved):
                    self.emit(f"        pop {register}")
            self.ax_clear()
            return
        handler = getattr(self, f"builtin_{name}", None)
        if handler is None:
            message = f"unknown function: {name}"
            raise CompileError(message, line=statement.line)
        clobbers = self.BUILTIN_CLOBBERS[name]
        saved = self._pinned_registers_to_save(clobbers)
        use_pusha = discard_return and len(saved) >= 3
        if use_pusha:
            self.emit("        pusha")
        else:
            for register in saved:
                self.emit(f"        push {register}")
        handler(arguments)
        if use_pusha:
            self.emit("        popa")
        else:
            for register in reversed(saved):
                self.emit(f"        pop {register}")

    def generate_do_while(self, statement: DoWhile, /) -> None:
        """Generate assembly for a do...while loop.

        The body executes unconditionally once, then the condition is
        tested at the bottom.  ``break`` inside the body jumps to the
        end label, same as in a ``while`` loop.  ``continue`` jumps to
        the condition test so the loop can re-evaluate and restart.
        """
        condition, body = statement.cond, statement.body
        label_index = self.new_label()
        end_label = f".do_{label_index}_end"
        continue_label = f".do_{label_index}_continue"
        self.emit(f".do_{label_index}:")
        self.loop_end_labels.append(end_label)
        self.loop_continue_labels.append(continue_label)
        self.generate_body(body, scoped=True)
        self.emit(f"{continue_label}:")
        # Short-circuit any false operand straight to end; otherwise
        # fall through to the unconditional jump back to the top.  The
        # ``jfalse end_label; jmp top; end_label:`` pattern is collapsed
        # by peephole_double_jump into ``jtrue top`` for single
        # comparisons.
        self.emit_condition_false_jump(condition=condition, fail_label=end_label, context="do_while")
        self.emit(f"        jmp .do_{label_index}")
        self.emit(f"{end_label}:")
        self.loop_continue_labels.pop()
        self.loop_end_labels.pop()

    def generate_expression(self, expression: Node, /) -> None:
        """Generate code for an expression, leaving the result in AX.

        Raises:
            CompileError: If an unknown expression kind or operator is encountered.

        """
        # Skip load if AX already holds this variable's value.
        if isinstance(expression, Var) and expression.name == self.ax_local:
            return
        if isinstance(expression, Int):
            self.ax_clear()
            if expression.value == 0:
                self.emit("        xor ax, ax")
            else:
                self.emit(f"        mov ax, {expression.value}")
        elif isinstance(expression, String):
            self.ax_clear()
            self.emit(f"        mov ax, {self.new_string_label(expression.content)}")
        elif isinstance(expression, Var):
            vname = expression.name
            if vname in self.NAMED_CONSTANTS:
                self.emit_constant_reference(vname)
                self.emit(f"        mov ax, {vname}")
                self.ax_clear()
                return
            if vname in self.constant_aliases:
                self.emit(f"        mov ax, {self.constant_aliases[vname]}")
                self.ax_clear()
                return
            if vname in self.global_arrays:
                # A global array name decays to its base address — the
                # ``_g_<name>`` label.  Load it as an immediate, not as a
                # memory fetch from that address.
                self.emit(f"        mov ax, _g_{vname}")
                self.ax_clear()
                return
            self._check_defined(vname, line=expression.line)
            if self.variable_types.get(vname) == "unsigned long":
                message = f"'unsigned long' variable {vname!r} cannot be used in a 16-bit expression context"
                raise CompileError(message, line=expression.line)
            if vname in self.pinned_register:
                self.emit(f"        mov ax, {self.pinned_register[vname]}")
            elif vname in self.register_aliased_globals:
                self.emit(f"        mov ax, {self.register_aliased_globals[vname]}")
            else:
                self.emit(f"        mov ax, [{self._local_address(vname)}]")
            self.ax_is_byte = False
            self.ax_local = vname
        elif isinstance(expression, Index):
            self.ax_clear()
            vname = expression.name
            index_expression = expression.index
            self._check_defined(vname, line=expression.line)
            if isinstance(index_expression, Int) and vname in self.array_labels:
                offset = index_expression.value * 2
                label = self.array_labels[vname]
                if offset:
                    self.emit(f"        mov ax, [{label}+{offset}]")
                else:
                    self.emit(f"        mov ax, [{label}]")
            elif isinstance(index_expression, Int):
                is_byte = self._is_byte_var(vname)
                offset = index_expression.value * (1 if is_byte else 2)
                # Direct memory access for constant/aliased bases:
                # emit `mov ax, [CONST+N]` instead of `mov bx, CONST / mov ax, [bx+N]`.
                const_base = self._resolve_constant(vname)
                if const_base is not None:
                    addr = f"{const_base}+{offset}" if offset else const_base
                    if is_byte:
                        self.emit(f"        mov al, [{addr}]")
                        self.emit("        xor ah, ah")
                    else:
                        self.emit(f"        mov ax, [{addr}]")
                else:
                    guarded = self._si_scratch_guard_begin(vname)
                    self._emit_load_var(vname, register="si")
                    if is_byte:
                        if offset:
                            self.emit(f"        mov al, [si+{offset}]")
                        else:
                            self.emit("        mov al, [si]")
                        self.emit("        xor ah, ah")
                    elif offset:
                        self.emit(f"        mov ax, [si+{offset}]")
                    else:
                        self.emit("        mov ax, [si]")
                    self._si_scratch_guard_end(guarded=guarded)
            else:
                is_byte = self._is_byte_var(vname)
                const_base = self._resolve_constant(vname)
                if const_base is not None:
                    self.emit_constant_reference(vname)
                    addr = self._emit_constant_base_index_addr(
                        const_base=const_base,
                        index=index_expression,
                        is_byte=is_byte,
                        preserve_ax=False,
                    )
                    if is_byte:
                        self.emit(f"        mov al, [{addr}]")
                        self.emit("        xor ah, ah")
                    else:
                        self.emit(f"        mov ax, [{addr}]")
                    self.ax_clear()
                else:
                    guarded = self._si_scratch_guard_begin(vname)
                    self._emit_load_var(vname, register="si")
                    # If the index is a pinned variable and the access is
                    # byte-sized, load it without clobbering SI.
                    if is_byte and isinstance(index_expression, Var) and index_expression.name in self.pinned_register:
                        self.emit(f"        add si, {self.pinned_register[index_expression.name]}")
                    elif isinstance(index_expression, (Var, Int)):
                        # Simple Var/Int load doesn't touch SI, so skip the
                        # push/pop round-trip.
                        self.generate_expression(index_expression)
                        if not is_byte:
                            self.emit("        add ax, ax")
                        self.emit("        add si, ax")
                    else:
                        self.emit("        push si")
                        self.generate_expression(index_expression)
                        if not is_byte:
                            self.emit("        add ax, ax")
                        self.emit("        pop si")
                        self.emit("        add si, ax")
                    if is_byte:
                        self.emit("        mov al, [si]")
                        self.emit("        xor ah, ah")
                    else:
                        self.emit("        mov ax, [si]")
                    self._si_scratch_guard_end(guarded=guarded)
                    # AX now holds the subscript result, not the index —
                    # invalidate the tracking that generate_expression set.
                    self.ax_clear()
        elif isinstance(expression, SizeofType):
            self.ax_clear()
            self.emit(f"        mov ax, {self.TYPE_SIZES[expression.type_name]}")
        elif isinstance(expression, SizeofVar):
            self.ax_clear()
            vname = expression.name
            if vname in self.global_arrays:
                declaration = self.global_arrays[vname]
                stride = 1 if declaration.type_name == "char" else 2
                if declaration.init is not None:
                    size = len(declaration.init.elements) * stride
                    self.emit(f"        mov ax, {size}")
                else:
                    size_expression = self._constant_expression(declaration.size)
                    self.emit(f"        mov ax, ({size_expression})*{stride}")
            elif vname in self.array_sizes:
                size = self.array_sizes[vname] * 2  # word-sized elements
                self.emit(f"        mov ax, {size}")
            else:
                size = 2  # all non-array variables are word-sized
                self.emit(f"        mov ax, {size}")
        elif isinstance(expression, Call):
            self.generate_call(expression)
        elif isinstance(expression, BinOp):
            # Fold an entirely-constant subtree (named constants and
            # integer literals) into a single ``mov ax, <expr>`` so the
            # assembler does the arithmetic.  Without this, expressions
            # like ``O_WRONLY + O_CREAT + O_TRUNC`` build the value at
            # runtime via push/pop chains.
            if (constant_expr := self._constant_expression(expression)) is not None:
                for name in self._collect_constant_references(expression):
                    self.emit_constant_reference(name)
                self.emit(f"        mov ax, {constant_expr}")
                self.ax_clear()
                return
            operator, left, right = expression.op, expression.left, expression.right
            if operator == "%" and self._has_remainder(left, right):
                self.emit("        mov ax, dx")
                self.ax_clear()
                return
            if operator in ("+", "-", "&", "|", "^") and isinstance(right, Int):
                # Fast path: reg op imm16 uses the immediate form, skipping
                # the mov-into-cx scratch step.  Saves 2-3 bytes per site.
                self.generate_expression(left)
                # +1 and -1 fit in a 1-byte inc/dec.
                if operator == "+" and right.value == 1:
                    self.emit("        inc ax")
                elif operator == "-" and right.value == 1:
                    self.emit("        dec ax")
                elif operator == "^" and (right.value & 0xFFFF) == 0xFFFF:
                    # ``x ^ 0xFFFF`` is the ``~x`` lowering — ``not ax``
                    # is 2 bytes vs. 3 for ``xor ax, 0xFFFF``.
                    self.emit("        not ax")
                else:
                    mnemonic = {"+": "add", "-": "sub", "&": "and", "|": "or", "^": "xor"}[operator]
                    self.emit(f"        {mnemonic} ax, {right.value}")
                self.ax_clear()
                return
            if operator == "<<" and isinstance(right, Int):
                shift = right.value & 0x1F
                # Fast path: shl r16, imm — one instruction, no CX scratch.
                self.generate_expression(left)
                if shift == 0:
                    pass
                elif shift >= 16:
                    self.emit("        xor ax, ax")
                else:
                    self.emit(f"        shl ax, {shift}")
                self.ax_clear()
                return
            if operator == ">>" and isinstance(right, Int):
                shift = right.value & 0x1F
                # Special case: `local >> 8` when ``local`` lives in memory.
                # Loading the high byte directly avoids one instruction
                # over `mov ax, [local]` + `shr ax, 8`, and doesn't waste
                # an ALU op on a shift that's really a byte-select.
                if (
                    shift == 8
                    and isinstance(left, Var)
                    and self._is_memory_scalar(left.name)
                    and left.name not in self.pinned_register
                    and left.name not in self.array_labels
                ):
                    self.emit(f"        mov al, [{self._local_address(left.name)}+1]")
                    self.emit("        xor ah, ah")
                    self.ax_clear()
                    return
                # Fast path: shr r16, imm — one instruction, no CX scratch.
                self.generate_expression(left)
                if shift == 0:
                    pass
                elif shift >= 16:
                    self.emit("        xor ax, ax")
                else:
                    self.emit(f"        shr ax, {shift}")
                self.ax_clear()
                return
            # Fast path for ``+``/``-`` with a stack-resident right operand:
            # ``add ax, [mem]`` is shorter than ``mov cx, [mem] / add ax, cx``.
            if (
                operator in ("+", "-")
                and isinstance(right, Var)
                and self._is_memory_scalar(right.name)
                and right.name not in self.pinned_register
                and right.name not in self.variable_arrays
                and self.variable_types.get(right.name) != "unsigned long"
            ):
                self.generate_expression(left)
                mnemonic = "add" if operator == "+" else "sub"
                self.emit(f"        {mnemonic} ax, [{self._local_address(right.name)}]")
                self.ax_clear()
                return
            # Fast path for ``+``/``-``/``&``/``|``/``^`` with a
            # pinned-register right operand: arithmetic targets the
            # register directly, skipping the `mov cx, <reg>` load and
            # any CX save/restore.  When the pinned register is CX,
            # require ``left`` to be a leaf so generate_expression
            # can't clobber it mid-compute.
            if operator in ("+", "-", "&", "|", "^") and isinstance(right, Var) and right.name in self.pinned_register:
                source = self.pinned_register[right.name]
                if source != "cx" or isinstance(left, (Int, Var, String)):
                    self.generate_expression(left)
                    mnemonic = {"+": "add", "-": "sub", "&": "and", "|": "or", "^": "xor"}[operator]
                    self.emit(f"        {mnemonic} ax, {source}")
                    self.ax_clear()
                    return
            cx_pinned_var = next(
                (name for name, register in self.pinned_register.items() if register == "cx"),
                None,
            )
            # Skip the CX save when an enclosing store is about to
            # overwrite CX anyway — its original value is dead.
            protect_cx = cx_pinned_var is not None and self.store_target_register != "cx"
            if protect_cx:
                self.emit("        push cx")
            self.emit_binary_operator_operands(left, right)  # AX = left, CX = right
            if operator == "+":
                self.emit("        add ax, cx")
            elif operator == "-":
                self.emit("        sub ax, cx")
            elif operator == "&":
                self.emit("        and ax, cx")
            elif operator == "|":
                self.emit("        or ax, cx")
            elif operator == "^":
                self.emit("        xor ax, cx")
            elif operator == "<<":
                self.emit("        shl ax, cl")
            elif operator == ">>":
                self.emit("        shr ax, cl")
            elif operator == "*":
                protect_dx = any(register == "dx" for register in self.pinned_register.values()) and self.store_target_register != "dx"
                if protect_dx:
                    self.emit("        push dx")
                self.emit("        mul cx")
                if protect_dx:
                    self.emit("        pop dx")
                self.division_remainder = None
            elif operator in {"/", "%"}:
                dx_pinned = any(register == "dx" for register in self.pinned_register.values())
                protect_dx = dx_pinned and self.store_target_register != "dx"
                if protect_dx:
                    self.emit("        push dx")
                self.emit("        xor dx, dx")
                self.emit("        div cx")
                if operator == "%":
                    self.emit("        mov ax, dx")
                if protect_dx:
                    self.emit("        pop dx")
                if dx_pinned:
                    self.division_remainder = None
                else:
                    self.division_remainder = (left, right)
            elif operator in JUMP_WHEN_FALSE:
                # Booleanize the comparison: AX = 1 if ``left <op> right``,
                # else 0.  ``mov ax, 0`` preserves the flags set by ``cmp``
                # (unlike ``xor ax, ax``), so the jump-when-false branch
                # reads the right condition.
                skip_label = f".bool_{self.new_label()}"
                self.emit("        cmp ax, cx")
                self.emit("        mov ax, 0")
                self.emit(f"        {JUMP_WHEN_FALSE[operator]} {skip_label}")
                self.emit("        inc ax")
                self.emit(f"{skip_label}:")
            else:
                message = f"unknown operator: {operator}"
                raise CompileError(message, line=expression.line)
            if protect_cx:
                self.emit("        pop cx")
            self.ax_clear()
        else:
            message = f"unknown expression: {type(expression).__name__}"
            raise CompileError(message, line=expression.line)

    def generate_function(self, function: Function, /) -> None:
        """Generate assembly for a single function definition."""
        name = function.name
        if function.always_inline:
            # No free-standing body; the function has been recorded in
            # ``inline_bodies`` and will be spliced at each call site.
            return
        parameters = function.params
        body = function.body
        self.array_labels = {}
        self.array_sizes = {}
        self.auto_pin_candidates: dict[str, str] = {}
        self.ax_clear()
        self.constant_aliases = {}
        self.current_carry_return = function.carry_return
        self.elide_frame = name == "main"
        # Frame-elide criteria for non-main functions.  The bp frame
        # becomes dead weight whenever the body makes no BP-relative
        # accesses: no parameters (no ``[bp+N]`` reads), no locals
        # (no ``[bp-N]`` slots), and no cc.py codegen path that
        # touches BP.  Inside a function body, ``asm("...")`` parses
        # as a ``Call`` to the ``asm`` builtin (not an InlineAsm
        # node — that's only used for file-scope ``asm(...)``
        # directives).
        #
        # ``naked_asm`` covers the hand-coded inline-asm helpers
        # (``emit_byte_al`` / ``skip_ws`` / ``resolve_value`` /
        # ``symbol_lookup``).  ``frameless_calls`` covers pure-C
        # dispatch helpers — handlers like ``handle_clc`` whose
        # body is ``emit_byte(0xF8);`` or ``handle_aam`` whose body
        # is two ``emit_byte(...)`` calls.  For those, cc.py's call
        # codegen emits ``mov ax, N ; call fn`` with no pin save
        # (no locals means no pinned registers) and no stack-arg
        # math, so BP is genuinely unused.
        naked_asm = name != "main" and not parameters and len(body) == 1 and isinstance(body[0], Call) and body[0].name == "asm"
        frameless_calls = (
            name != "main" and not parameters and len(body) >= 1 and all(isinstance(stmt, Call) and stmt.name != "asm" for stmt in body)
        )
        if naked_asm or frameless_calls:
            self.elide_frame = True
        self.frame_size = 0
        self.live_long_local = None
        self.locals = {}
        self.pinned_register = {}
        self.variable_arrays = set()
        self.variable_types = {}
        self.virtual_long_locals = set()
        self.zero_init_skippable: set[str] = set()

        # Globals are visible in every function.  Scalars get a
        # ``_g_<name>`` memory slot; arrays are resolved via the
        # ``_resolve_constant`` path (they behave like a fixed base
        # address, word-strided for ``int`` and byte-strided for
        # ``char``).
        for global_name, declaration in self.global_scalars.items():
            self.variable_types[global_name] = declaration.type_name
            self.visible_vars.add(global_name)
        for global_name, declaration in self.global_arrays.items():
            self.variable_types[global_name] = declaration.type_name
            self.variable_arrays.add(global_name)
            self.visible_vars.add(global_name)

        # Fastcall (regparm(1)) routing.  Param 0 arrives in AX and is
        # spilled to a local stack slot during the prologue; params 1..N
        # use the standard caller-pushed cdecl layout shifted down by
        # one slot (caller didn't push arg 0).
        is_fastcall = name != "main" and function.regparm_count > 0
        # Allocate parameters and record their types.
        for param in parameters:
            if param.name in self.global_scalars or param.name in self.global_arrays:
                message = f"parameter '{param.name}' shadows a file-scope global"
                raise CompileError(message, line=function.line)
        if name == "main":
            # main parameters are handled by emit_argument_vector_startup.
            for param in parameters:
                self.allocate_local(param.name)
                self.variable_types[param.name] = param.type
                if param.is_array:
                    self.variable_arrays.add(param.name)
        else:
            # Non-main: record parameter types; stack offsets are kept
            # as fallbacks but parameters will be pinned to registers
            # when safe_pin_registers has room.
            for i, param in enumerate(parameters):
                self.variable_types[param.name] = param.type
                if param.is_array:
                    self.variable_arrays.add(param.name)
                if is_fastcall and i == 0:
                    # Param 0 gets a local slot allocated below; it has no
                    # caller-pushed address.
                    continue
                stack_index = i - 1 if is_fastcall else i
                self.locals[param.name] = -(4 + stack_index * 2)  # negative = above bp

        self.discover_virtual_long_locals(body)
        self.safe_pin_registers = self.compute_safe_pin_registers(body)
        # Exclude fastcall param 0 from auto-pin candidates — it's spilled to
        # the stack at prologue entry and the body accesses it through that
        # slot like any other local.
        if name == "main":
            param_candidates = []
        elif is_fastcall:
            param_candidates = parameters[1:]
        else:
            param_candidates = parameters
        self.auto_pin_candidates = self._select_auto_pin_candidates(body=body, parameters=param_candidates)

        # Reserve a local stack slot for fastcall param 0 before scan_locals
        # runs so its offset is stable against body-local allocations.
        if is_fastcall:
            self.allocate_local(parameters[0].name)

        self.scan_locals(body)

        # Non-main: pin parameters that won a candidate slot but weren't
        # claimed during scan_locals.  Parameters that don't fit stay on
        # the stack at [bp+N].
        if name != "main":
            for i, param in enumerate(parameters):
                if is_fastcall and i == 0:
                    continue
                if param.name not in self.auto_pin_candidates or param.name in self.pinned_register:
                    continue
                self.pinned_register[param.name] = self.auto_pin_candidates[param.name]

        # Seed visible_vars with parameters and pinned variables.
        # Block-scoped locals become visible when their declaration
        # is reached during code generation.
        for param in parameters:
            self.visible_vars.add(param.name)
        self.visible_vars.update(self.pinned_register)

        # Register calling convention: pinned parameters arrive in their
        # target register (caller loaded them before the call), and
        # non-pinned parameters keep compact [bp+N] offsets that skip
        # register-passed slots.
        register_convention = name != "main" and name in self.register_convention_functions
        if register_convention:
            stack_position = 0
            for param in parameters:
                if param.name in self.pinned_register:
                    continue
                self.locals[param.name] = -(4 + stack_position * 2)
                stack_position += 1

        self.emit(f"{name}:")
        if not self.elide_frame:
            self.emit("        push bp")
            self.emit("        mov bp, sp")
            if self.frame_size > 0:
                self.emit(f"        sub sp, {self.frame_size}")
            if is_fastcall:
                # Spill AX (the caller-supplied arg 0) into its local slot
                # so the body can read it through the normal local path.
                slot = self.locals[parameters[0].name]
                self.emit(f"        mov [bp-{slot}], ax")
            if not register_convention:
                # Load pinned parameters from caller-pushed stack slots
                # into their registers.
                for i, param in enumerate(parameters):
                    if is_fastcall and i == 0:
                        continue
                    if param.name in self.pinned_register:
                        register = self.pinned_register[param.name]
                        stack_index = i - 1 if is_fastcall else i
                        self.emit(f"        mov {register}, [bp+{4 + stack_index * 2}]")

        # Emit argc/argv startup for main with parameters.
        if name == "main" and parameters:
            body = self.emit_argument_vector_startup(parameters, body=body)

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
        elif self.elide_frame:
            # naked_asm and frameless_calls both skip the prologue, so
            # the epilogue is just ``ret`` — no ``pop bp`` because we
            # didn't push it.
            self.emit("        ret")
        elif not self.always_exits(body):
            if self.frame_size > 0:
                self.emit("        mov sp, bp")
            self.emit("        pop bp")
            self.emit("        ret")
        self.emit()

    def generate_if(self, statement: If, /) -> None:
        """Generate assembly for an if statement.

        Before emitting anything, checks whether this if begins a
        ``var op literal`` dispatch chain over a memory-resident local
        (e.g. ``if (c == 1) … else if (c == 2) …``).  If so and AX
        does not already hold the local, hoists a single
        ``mov ax, [_l_var]`` so every subsequent comparison along the
        chain uses the 3-byte ``cmp ax, imm`` form instead of a
        6-byte ``cmp word [mem], imm``.  The else-label snapshot logic
        below preserves AX-tracking through each branch so the chain
        keeps reusing the same load.
        """
        condition, body, else_body = statement.cond, statement.body, statement.else_body
        chain_var = self._dispatch_chain_var(statement)
        if chain_var is not None and chain_var != self.ax_local:
            self.emit(f"        mov ax, [{self._local_address(chain_var)}]")
            self.ax_local = chain_var
            self.ax_is_byte = False
        label_index = self.new_label()
        if else_body is not None:
            self.emit_condition_false_jump(condition=condition, fail_label=f".if_{label_index}_else", context="if")
            # Snapshot AX tracking at the point the fall-through (else)
            # path actually resumes — before body generation disturbs it.
            post_condition_ax = (self.ax_local, self.ax_is_byte)
            self.generate_body(body, scoped=True)
            if_exits = self.always_exits(body)
            if not if_exits:
                self.emit(f"        jmp .if_{label_index}_end")
            self.emit(f".if_{label_index}_else:")
            self.ax_local, self.ax_is_byte = post_condition_ax
            self.generate_body(else_body, scoped=True)
            if not if_exits or not self.always_exits(else_body):
                self.emit(f".if_{label_index}_end:")
            self.ax_clear()
        else:
            self.emit_condition_false_jump(condition=condition, fail_label=f".if_{label_index}_end", context="if")
            post_condition_ax = (self.ax_local, self.ax_is_byte)
            self.generate_body(body, scoped=True)
            self.emit(f".if_{label_index}_end:")
            # If the body always exits its enclosing block (via die,
            # exit, return, or break), the fall-through path resumes
            # with AX tracking as of the end of condition evaluation.
            if self.always_exits(body):
                self.ax_local, self.ax_is_byte = post_condition_ax
            else:
                self.ax_clear()

    def generate_index_assign(self, statement: IndexAssign, /) -> None:
        """Generate assembly for ``name[index] = expr;``.

        When the base pointer lives in memory (not a named constant) and
        a different ``asm_register("si")`` global is active, loading the
        base into SI would clobber that alias — the SI scratch guard
        wraps the store with ``push si`` / ``pop si`` to preserve the
        pinned value.  Matches the read-side guard in generate_expression.
        """
        self.ax_clear()
        name = statement.name
        is_byte = self._is_byte_var(name)
        self._check_defined(name, line=statement.line)
        # Evaluate value into AX, then store at base+index.
        if isinstance(statement.index, Int) and isinstance(statement.expr, Int):
            # Both index and value are constants: direct store.
            offset = statement.index.value * (1 if is_byte else 2)
            const_base = self._resolve_constant(name)
            if const_base is not None:
                addr = f"{const_base}+{offset}" if offset else const_base
                guarded = False
            else:
                guarded = self._si_scratch_guard_begin(name)
                self._emit_load_var(name, register="si")
                addr = f"si+{offset}" if offset else "si"
            if is_byte:
                self.emit(f"        mov byte [{addr}], {statement.expr.value}")
            else:
                self.emit(f"        mov word [{addr}], {statement.expr.value}")
            self._si_scratch_guard_end(guarded=guarded)
        elif isinstance(statement.index, Int):
            # Constant index, variable value.
            offset = statement.index.value * (1 if is_byte else 2)
            self.generate_expression(statement.expr)
            const_base = self._resolve_constant(name)
            if const_base is not None:
                addr = f"{const_base}+{offset}" if offset else const_base
                guarded = False
            else:
                guarded = self._si_scratch_guard_begin(name)
                self._emit_load_var(name, register="si")
                addr = f"si+{offset}" if offset else "si"
            if is_byte:
                self.emit(f"        mov [{addr}], al")
            else:
                self.emit(f"        mov [{addr}], ax")
            self._si_scratch_guard_end(guarded=guarded)
        else:
            const_base = self._resolve_constant(name)
            if const_base is not None:
                self.emit_constant_reference(name)
                self.generate_expression(statement.expr)
                addr = self._emit_constant_base_index_addr(
                    const_base=const_base,
                    index=statement.index,
                    is_byte=is_byte,
                    preserve_ax=True,
                )
                if is_byte:
                    self.emit(f"        mov [{addr}], al")
                else:
                    self.emit(f"        mov [{addr}], ax")
                self.ax_clear()
            else:
                # Variable index: compute address in SI, then store.
                # Guard goes OUTSIDE the push/pop ax pair so the pop
                # order matches the push order (push ax..., pop ax, pop si).
                guarded = self._si_scratch_guard_begin(name)
                self.generate_expression(statement.expr)
                self.emit("        push ax")
                self._emit_load_var(name, register="si")
                # If the index is a simple Var/Int, evaluating it doesn't
                # clobber SI, so we can skip the push/pop round-trip.
                if isinstance(statement.index, (Var, Int)):
                    self.generate_expression(statement.index)
                    if not is_byte:
                        self.emit("        add ax, ax")
                    self.emit("        add si, ax")
                else:
                    self.emit("        push si")
                    self.generate_expression(statement.index)
                    if not is_byte:
                        self.emit("        add ax, ax")
                    self.emit("        pop si")
                    self.emit("        add si, ax")
                self.emit("        pop ax")
                # After pop, AX holds the value being stored, not the index —
                # invalidate the ax_local tracking that generate_expression set.
                self.ax_clear()
                if is_byte:
                    self.emit("        mov [si], al")
                else:
                    self.emit("        mov [si], ax")
                self._si_scratch_guard_end(guarded=guarded)

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
                raise CompileError(message, line=expression.line)
            if vname in self.virtual_long_locals:
                if self.live_long_local != vname:
                    message = f"internal: virtual long {vname!r} consumed when not live"
                    raise CompileError(message, line=expression.line)
                self.live_long_local = None
                return
            address = self._local_address(vname)
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
        raise CompileError(message, line=expression.line)

    def generate_return(self, statement: Return, /) -> None:
        """Generate assembly for a return statement.

        In ``main``, ``return`` maps to ``jmp FUNCTION_EXIT``.  In other
        functions it evaluates the return expression into AX, tears down
        the stack frame, and emits ``ret``.  For ``carry_return``
        functions, ``return 1`` / ``return 0`` bypass AX entirely and
        set CF instead (``clc`` / ``stc``); any other return value is
        rejected at codegen time.
        """
        if self.elide_frame:
            # main: return [expr]; → exit() (discard return value)
            self.emit("        jmp FUNCTION_EXIT")
            return
        if self.current_carry_return:
            if not isinstance(statement.value, Int) or statement.value.value not in (0, 1):
                message = "carry_return functions may only ``return 0`` (stc, false) or ``return 1`` (clc, true)"
                raise CompileError(message, line=statement.line)
            self.emit("        clc" if statement.value.value == 1 else "        stc")
            if self.frame_size > 0:
                self.emit("        mov sp, bp")
            self.emit("        pop bp")
            self.emit("        ret")
            return
        if statement.value is not None:
            self.generate_expression(statement.value)
        if self.frame_size > 0:
            self.emit("        mov sp, bp")
        self.emit("        pop bp")
        self.emit("        ret")

    def generate_statement(self, statement: Node, /) -> None:
        """Generate assembly for a single statement.

        Raises:
            CompileError: If an unknown statement kind is encountered.

        """
        if isinstance(statement, VarDecl):
            self.visible_vars.add(statement.name)
            self.variable_types[statement.name] = statement.type_name
            if statement.name in self.constant_aliases:
                return
            if statement.init is not None:
                if statement.name in self.zero_init_skippable:
                    self.zero_init_skippable.discard(statement.name)
                else:
                    self.emit_store_local(expression=statement.init, name=statement.name)
        elif isinstance(statement, ArrayDecl):
            self.visible_vars.add(statement.name)
            self.variable_types[statement.name] = statement.type_name
            if statement.init is not None:
                elem_labels = []
                for elem in statement.init.elements:
                    if isinstance(elem, String):
                        elem_labels.append(self.new_string_label(elem.content))
                    elif isinstance(elem, Int):
                        elem_labels.append(str(elem.value))
                    else:
                        message = "array initializer elements must be constants"
                        raise CompileError(message, line=elem.line)
                array_label = f"_arr_{len(self.arrays)}"
                self.arrays.append((array_label, elem_labels))
                self.array_labels[statement.name] = array_label
                self.array_sizes[statement.name] = len(elem_labels)
                self.emit(f"        mov word [{self._local_address(statement.name)}], {array_label}")
        elif isinstance(statement, Assign):
            self._check_defined(statement.name, line=statement.line)
            self.emit_store_local(expression=statement.expr, name=statement.name)
        elif isinstance(statement, IndexAssign):
            self.generate_index_assign(statement)
        elif isinstance(statement, Break):
            if not self.loop_end_labels:
                message = "break outside of a loop"
                raise CompileError(message, line=statement.line)
            self.emit(f"        jmp {self.loop_end_labels[-1]}")
        elif isinstance(statement, Continue):
            if not self.loop_continue_labels:
                message = "continue outside of a loop"
                raise CompileError(message, line=statement.line)
            self.emit(f"        jmp {self.loop_continue_labels[-1]}")
        elif isinstance(statement, DoWhile):
            self.ax_clear()
            self.generate_do_while(statement)
        elif isinstance(statement, If):
            self.generate_if(statement)
        elif isinstance(statement, While):
            self.ax_clear()
            self.generate_while(statement)
        elif isinstance(statement, Return):
            self.generate_return(statement)
        elif isinstance(statement, Call):
            self.generate_call(statement, discard_return=True)
            self.ax_clear()
        else:
            message = f"unknown statement: {type(statement).__name__}"
            raise CompileError(message, line=statement.line)

    def generate_while(self, statement: While, /) -> None:
        """Generate assembly for a while loop.

        ``while (1)`` and other statically-nonzero conditions skip the
        header check entirely.  The end label is still emitted so a
        ``break`` statement inside the body has a target; when no
        ``break`` is present the label is dead and costs nothing.
        ``continue`` jumps back to the loop header, re-running the
        condition test.
        """
        condition, body = statement.cond, statement.body
        label_index = self.new_label()
        end_label = f".while_{label_index}_end"
        top_label = f".while_{label_index}"
        self.emit(f"{top_label}:")
        self.loop_end_labels.append(end_label)
        self.loop_continue_labels.append(top_label)
        if self._is_constant_true_condition(condition):
            self.generate_body(body, scoped=True)
        else:
            self.emit_condition_false_jump(condition=condition, fail_label=end_label, context="while")
            self.generate_body(body, scoped=True)
        self.emit(f"        jmp {top_label}")
        self.emit(f"{end_label}:")
        self.loop_continue_labels.pop()
        self.loop_end_labels.pop()
        # A ``break`` can exit the loop with AX holding a value other
        # than the one the final iteration's ``ax_local`` tracking
        # would predict (e.g. ``break`` inside ``char *prev = end - 1;
        # if (prev[0] != ' ') break;`` leaves AX = prev, not end).
        # Invalidate ax_local so downstream code reloads from memory.
        self.ax_clear()

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
        """Run peephole optimization passes over generated assembly.

        Ordering note: :meth:`peephole_memory_arithmetic` runs before
        :meth:`peephole_store_reload` so that load/modify/store triples
        get folded into a direct ``inc D`` (etc.) first.  Reversing the
        order lets ``store_reload`` delete a reload that ``emit_store_local``
        added as a safety net — ``memory_arithmetic`` would then fuse
        the triple and leave the downstream read picking up stale AX.
        """
        self.peephole_dead_code()
        self.peephole_double_jump()
        self.peephole_jump_next()
        self.peephole_label_forwarding()
        self.peephole_memory_arithmetic()
        self.peephole_store_reload()
        self.peephole_dx_to_memory()
        self.peephole_constant_to_register()
        self.peephole_register_arithmetic()
        self.peephole_index_through_memory()
        self.peephole_fold_zero_save()
        self.peephole_compare_through_register()
        self.peephole_dead_ah()
        self.peephole_unused_cld()
        self.peephole_dead_stores()
        self.peephole_dead_test_after_sbb()
        self.peephole_redundant_bx()

    def peephole_compare_through_register(self) -> None:
        """Fold ``mov ax, <reg> / cmp ax, <X>`` into ``cmp <reg>, <X>``.

        When the cmp's left operand is already in a 16-bit register,
        the rebinding through AX is just to satisfy the existing
        ``emit_comparison`` template that always lands the left
        operand in AX.  ``cmp r16, r16`` and ``cmp r16, [mem]`` are
        the same length as the AX-flavored forms, so deleting the
        2-byte ``mov ax, <reg>`` is pure win.

        Only applied when the instruction after the cmp is a
        conditional jump — that's the only context where AX's value
        is provably dead after the cmp (the cmp itself doesn't write
        AX, but subsequent fall-through code might consume the
        rebinding).
        """
        registers = {"bx", "cx", "dx", "si", "di", "bp"}
        jump_prefixes = (
            "ja ",
            "jae ",
            "jb ",
            "jbe ",
            "jc ",
            "je ",
            "jg ",
            "jge ",
            "jl ",
            "jle ",
            "jnc ",
            "jne ",
            "jno ",
            "jnp ",
            "jns ",
            "jnz ",
            "jo ",
            "jp ",
            "js ",
            "jz ",
        )
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith("mov ax, "):
                i += 1
                continue
            source = a[len("mov ax, ") :]
            if source not in registers:
                i += 1
                continue
            if not b.startswith("cmp ax, "):
                i += 1
                continue
            if not any(c.startswith(prefix) for prefix in jump_prefixes):
                i += 1
                continue
            rhs = b[len("cmp ax, ") :]
            self.lines[i] = f"        cmp {source}, {rhs}"
            del self.lines[i + 1]

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
        # Collect all _l_ labels referenced anywhere except as a store
        # destination.  Stores are "mov ... [_l_X], <src>"; reads include
        # "mov <dst>, [_l_X]", "cmp word [_l_X], ...", etc.
        loaded: set[str] = set()
        for line in self.lines:
            stripped = line.strip()
            if self._extract_local_label(stripped) is not None:
                continue
            cursor = 0
            while True:
                start = stripped.find("[_l_", cursor)
                if start < 0:
                    break
                # Extract the bare label — stop at the first non-identifier
                # byte. `[_l_sum+1]` must count as a reference to `_l_sum`,
                # not `_l_sum+1`.
                label_end = start + 1
                while label_end < len(stripped) and (stripped[label_end].isalnum() or stripped[label_end] == "_"):
                    label_end += 1
                loaded.add(stripped[start + 1 : label_end])
                cursor = stripped.index("]", label_end) + 1
        # Remove stores and declarations for labels never loaded.
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            label = self._extract_local_label(stripped)
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
        The ``.L1:`` label is kept when other jumps still target it;
        deleting it would leave those references dangling.
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
                label = parts[1]
                self.lines[i] = f"        {JUMP_INVERT[parts[0]]} {target}"
                label_referenced_elsewhere = any(
                    j != i and j != i + 1 and j != i + 2 and (tokens := self.lines[j].split()) and len(tokens) >= 2 and tokens[-1] == label
                    for j in range(len(self.lines))
                )
                if label_referenced_elsewhere:
                    del self.lines[i + 1]
                else:
                    del self.lines[i + 1 : i + 3]
                continue
            i += 1

    def peephole_dx_to_memory(self) -> None:
        """Fold ``mov ax, dx / mov [X], ax`` into ``mov [X], dx``.

        The pair arises after a ``%`` expression whose remainder the
        ``%`` handler stages into AX just so the standard store path
        can flush it to the local — but the intermediate AX hop is
        dead if the next instruction writes that memory anyway.
        """
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a == "mov ax, dx" and b.startswith("mov [") and b.endswith("], ax"):
                self.lines[i + 1] = f"{self.lines[i + 1][:-3]}dx"
                del self.lines[i]
                continue
            i += 1

    def peephole_fold_zero_save(self) -> None:
        """Fuse ``xor reg, reg / push reg`` into ``push 0``.

        When ``cursor_column = 0`` is immediately followed by code that
        clobbers the pinned CX as scratch (and therefore needs to
        push/pop it), the compiler emits ``xor cx, cx / push cx``
        followed later by ``pop cx`` to restore zero.  The two-byte
        ``xor cx, cx`` plus one-byte ``push cx`` (3 bytes) collapses
        to a single two-byte ``push 0`` (``6A 00``) — the body and the
        eventual ``pop cx`` are unchanged, since the popped value is
        still zero.

        The xor's flag-side-effects are dead in every emission path
        cc.py produces here: ``push cx`` doesn't read flags and the
        intervening body overwrites them before the next conditional
        jump.
        """
        registers = {"ax", "bx", "cx", "dx", "si", "di", "bp"}
        i = 0
        while i < len(self.lines) - 1:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            if a.startswith("xor ") and " " in a[4:]:
                parts = a[4:].split(", ")
                if len(parts) == 2 and parts[0] == parts[1] and parts[0] in registers and b == f"push {parts[0]}":
                    self.lines[i] = "        push 0"
                    del self.lines[i + 1]
                    continue
            i += 1

    def peephole_index_through_memory(self) -> None:
        """Use ``add si, [mem]`` instead of staging through AX.

        Recognizes::

            push ax
            mov si, [BASE]
            mov ax, [INDEX]
            add si, ax
            pop ax

        and rewrites it as::

            mov si, [BASE]
            add si, [INDEX]

        Safe because the eight-byte form ``add si, [mem]`` is a single
        8086 instruction and AX is never disturbed.  Saves the
        push/pop AX pair (2 bytes) and the redundant ``mov ax, [mem]``
        (3 bytes) for a net 3-byte gain (the new ``add si, [mem]`` is
        2 bytes longer than the old ``add si, ax``).
        """
        i = 0
        while i < len(self.lines) - 4:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            d = self.lines[i + 3].strip()
            e = self.lines[i + 4].strip()
            if a != "push ax" or e != "pop ax":
                i += 1
                continue
            if not (b.startswith("mov si, [") and b.endswith("]")):
                i += 1
                continue
            if not (c.startswith("mov ax, [") and c.endswith("]")):
                i += 1
                continue
            if d != "add si, ax":
                i += 1
                continue
            mem_operand = c[len("mov ax, ") :]
            self.lines[i] = self.lines[i + 1]
            self.lines[i + 1] = f"        add si, {mem_operand}"
            del self.lines[i + 2 : i + 5]
            continue

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
        # Second pass: 3-instruction pattern without CX intermediate.
        # Handles four shapes of ``D = D <op> Y`` where D is memory or
        # a 16-bit register:
        #   mov ax, D / (add|sub|and) ax, imm  / mov D, ax → op D, imm
        #   mov ax, D / inc ax  / mov D, ax                → inc D
        #   mov ax, D / dec ax  / mov D, ax                → dec D
        #   mov ax, D / (add|sub|and) ax, <reg> / mov D, ax → op D, <reg>
        mnemonic_ops = {"add", "sub", "and", "or", "xor"}
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith("mov ax, "):
                i += 1
                continue
            source = a[len("mov ax, ") :]
            is_memory = source.startswith("[") and source.endswith("]")
            is_register = source in registers
            if not (is_memory or is_register):
                i += 1
                continue
            operator = None
            operand = None
            if b == "inc ax":
                operator = "inc"
                operand = ""
            elif b == "dec ax":
                operator = "dec"
                operand = ""
            else:
                for op in mnemonic_ops:
                    prefix = f"{op} ax, "
                    if b.startswith(prefix):
                        operator = op
                        operand = b[len(prefix) :]
                        break
            if operator is None:
                i += 1
                continue
            # Reject memory operands — would need swapping to ``mov ax, [X] /
            # op D, ax`` and handled by the next pass instead.
            if operand.startswith("["):
                i += 1
                continue
            if c != f"mov {source}, ax":
                i += 1
                continue
            width = "word " if is_memory else ""
            if operator in ("inc", "dec"):
                self.lines[i] = f"        {operator} {width}{source}"
            elif operand == "1" and operator in ("add", "sub"):
                instruction = "inc" if operator == "add" else "dec"
                self.lines[i] = f"        {instruction} {width}{source}"
            else:
                self.lines[i] = f"        {operator} {width}{source}, {operand}"
            del self.lines[i + 1 : i + 3]
            continue
        # Third pass: ``D = D <op> [X]`` with both sides in memory.
        # ``mov ax, D / op ax, [X] / mov D, ax`` collapses to
        # ``mov ax, [X] / op D, ax`` (10 bytes → 7 for word ops).  Only
        # safe when D is memory (the target of ``op D, ax`` must be
        # addressable as r/m16) and D ≠ X (overlapping would read the
        # stale value after the op writes D).
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith("mov ax, "):
                i += 1
                continue
            source = a[len("mov ax, ") :]
            if not (source.startswith("[") and source.endswith("]")):
                i += 1
                continue
            operator = None
            rhs = None
            for op in ("add", "sub", "and", "or", "xor"):
                prefix = f"{op} ax, "
                if b.startswith(prefix):
                    operator = op
                    rhs = b[len(prefix) :]
                    break
            if operator is None:
                i += 1
                continue
            if not (rhs.startswith("[") and rhs.endswith("]")):
                i += 1
                continue
            if rhs == source:
                i += 1
                continue
            if c != f"mov {source}, ax":
                i += 1
                continue
            self.lines[i] = f"        mov ax, {rhs}"
            self.lines[i + 1] = f"        {operator} {source}, ax"
            del self.lines[i + 2]
            continue

    def peephole_redundant_bx(self) -> None:
        """Remove redundant ``mov bx, X`` / ``mov si, X`` reloads.

        Tracks the value in each scratch register across instructions
        that don't clobber it (comparisons, conditional jumps).  Resets
        on labels, calls, interrupts, and any instruction that writes
        to the register.  BX and SI are both subscript scratch targets
        so either can linger with a useful value across sites.
        """
        self._dedup_register_reloads("bx")
        self._dedup_register_reloads("si")

    def peephole_register_arithmetic(self) -> None:
        """Compute directly into a pinned-local target register.

        Turns ``mov ax, X / <op> ax, Y / mov <reg>, ax`` into
        ``mov <reg>, X / <op> <reg>, Y`` when <reg> isn't already
        read by Y (e.g., ``sub reg, reg`` would zero it).

        Saves the trailing ``mov <reg>, ax`` (2 bytes) whenever the
        arithmetic result is being piped straight into a register
        (typically a pinned local).  After the transform AX retains
        whatever it held before the sequence, which is safe because
        pinned-register locals aren't referenced via AX tracking
        post-codegen.
        """
        registers = {"bx", "cx", "dx", "si", "di", "bp"}
        ops = ("add ax,", "sub ax,", "and ax,", "or ax,", "xor ax,")
        i = 0
        while i < len(self.lines) - 2:
            a = self.lines[i].strip()
            b = self.lines[i + 1].strip()
            c = self.lines[i + 2].strip()
            if not a.startswith("mov ax, "):
                i += 1
                continue
            if not any(b.startswith(op) for op in ops):
                i += 1
                continue
            if not c.startswith("mov "):
                i += 1
                continue
            parts = c[len("mov ") :].split(", ")
            if len(parts) != 2 or parts[1] != "ax" or parts[0] not in registers:
                i += 1
                continue
            target = parts[0]
            # Skip when the operand of the arithmetic references the
            # target register — rewriting would make it self-referential.
            operand = b.split(", ", 1)[1]
            if target in operand.split():
                i += 1
                continue
            source = a[len("mov ax, ") :]
            if target in source.split():
                i += 1
                continue
            new_op = b.replace("ax,", f"{target},", 1)
            self.lines[i] = f"        mov {target}, {source}"
            self.lines[i + 1] = f"        {new_op}"
            del self.lines[i + 2]
            continue

    def _dedup_register_reloads(self, register: str, /) -> None:
        """Skip ``mov {register}, <src>`` when ``<src>`` already reached this register.

        The tracked source goes stale on anything that changes either
        the register itself (direct clobber) or the source register
        when ``<src>`` is register-sourced — e.g. ``mov si, ax / inc
        ax / mov si, ax`` is NOT a redundant reload because ``inc ax``
        makes the second ``mov si, ax`` store a different value.
        Memory / immediate sources stay stable until the destination
        register is clobbered.
        """
        value: str | None = None
        result: list[str] = []
        # Instructions that clobber the destination register directly.
        clobber_prefixes = (
            f"add {register}",
            "call ",
            "int ",
            "lodsb",
            "lodsw",
            "movsb",
            "movsw",
            f"pop {register}",
            "rep ",
            f"sub {register}",
            "xchg",
            f"xor {register}",
        )
        # Register-modifying mnemonics we care about as SOURCE clobbers.
        # ``mov <reg>, X`` is handled below alongside the other writers.
        source_clobber_ops = (
            "add ",
            "and ",
            "dec ",
            "div ",
            "idiv ",
            "imul ",
            "inc ",
            "mov ",
            "mul ",
            "neg ",
            "not ",
            "or ",
            "rcl ",
            "rcr ",
            "rol ",
            "ror ",
            "sal ",
            "sar ",
            "shl ",
            "shr ",
            "sub ",
            "xor ",
        )
        for line in self.lines:
            stripped = line.strip()
            if stripped.startswith(f"mov {register}, "):
                source = stripped[len(f"mov {register}, ") :]
                if source == value:
                    continue  # redundant — skip
                value = source
            elif stripped.endswith(":") or stripped.startswith(clobber_prefixes):
                value = None
            elif value is not None and "[" not in value:
                # Source is a register or immediate.  Check whether this
                # instruction writes to the source register, invalidating
                # the stored value.  e.g. ``mov si, ax / inc ax`` — the
                # tracked ``ax`` in SI no longer matches the current AX.
                for op in source_clobber_ops:
                    if not stripped.startswith(op):
                        continue
                    target = stripped[len(op) :].split(",", 1)[0].strip()
                    if target == value or (len(target) == 2 and target[1] in "lh" and target[0] == value[0]):
                        value = None
                    break
            result.append(line)
        self.lines = result

    def peephole_store_reload(self) -> None:
        """Remove redundant store-then-reload sequences.

        Looks for ``mov [ADDR], ax`` followed (possibly across
        AX-preserving instructions like ``cmp``, ``test``, conditional
        jumps, or pushes/pops of non-AX registers) by ``mov ax, [ADDR]``
        — the reload is dead.  Stops scanning when it hits an
        instruction that could change AX, ``[ADDR]``, or control flow
        in a way that lets a different value reach the reload.
        """
        skip_prefixes = (
            "cmp ",
            "test ",
            "ja ",
            "jae ",
            "jb ",
            "jbe ",
            "jc ",
            "je ",
            "jg ",
            "jge ",
            "jl ",
            "jle ",
            "jnc ",
            "jne ",
            "jno ",
            "jnp ",
            "jns ",
            "jnz ",
            "jo ",
            "jp ",
            "js ",
            "jz ",
        )
        non_ax_pushpop = {f"{op} {reg}" for op in ("push", "pop") for reg in ("bx", "cx", "dx", "si", "di", "bp")}
        i = 0
        while i < len(self.lines) - 1:
            line = self.lines[i].strip()
            if not (line.startswith("mov [") and line.endswith(("], ax", "], al"))):
                i += 1
                continue
            address = line[4 : line.index("]") + 1]
            reload_word = f"mov ax, {address}"
            reload_byte = f"mov al, {address}"
            j = i + 1
            removed = False
            while j < len(self.lines):
                candidate = self.lines[j].strip()
                if candidate in (reload_word, reload_byte):
                    del self.lines[j]
                    removed = True
                    break
                # AX-preserving instructions: cmp/test/Jcc and pushes/pops
                # of registers other than AX.
                if any(candidate.startswith(prefix) for prefix in skip_prefixes) or candidate in non_ax_pushpop:
                    j += 1
                    continue
                break
            if removed:
                continue
            i += 1

    def peephole_unused_cld(self) -> None:
        """Remove or deduplicate ``cld`` instructions.

        When no string instruction is emitted, all ``cld`` instructions
        are removed.  Otherwise, redundant ``cld`` instructions are
        removed when the direction flag is already clear (no intervening
        label, call, or interrupt that could change DF).
        """
        string_ops = ("lodsb", "lodsw", "stosb", "stosw", "movsb", "movsw", "scasb", "scasw", "cmpsb", "cmpsw", "rep ")
        has_string_ops = any(any(line.strip().startswith(op) for op in string_ops) for line in self.lines)
        if not has_string_ops:
            self.lines = [line for line in self.lines if line.strip() != "cld"]
            return
        # Deduplicate: track whether DF is known-clear.
        df_clear = False
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip()
            if stripped == "cld":
                if df_clear:
                    continue  # redundant
                df_clear = True
            elif stripped.endswith(":") or stripped.startswith(("call ", "int ")):
                df_clear = False
            result.append(line)
        self.lines = result

    def scan_locals(self, statements: list[Node], /, *, top_level: bool = True) -> None:
        """Recursively find variable declarations.

        Plain ``int`` declarations are auto-pinned to a CPU register
        (from :data:`REGISTER_POOL`) when the declaration was chosen
        by :meth:`_select_auto_pin_candidates` and a slot is still
        available.  Slots are assigned in declaration order among
        selected candidates.  Call initializers stay in memory so
        they can participate in error-fusion optimizations without
        clobbering a pin.
        """
        for index, statement in enumerate(statements):
            if isinstance(statement, (VarDecl, ArrayDecl)) and (
                statement.name in self.global_scalars or statement.name in self.global_arrays
            ):
                message = f"local '{statement.name}' shadows a file-scope global"
                raise CompileError(message, line=statement.line)
            if isinstance(statement, VarDecl):
                self.variable_types[statement.name] = statement.type_name
                if top_level and self._is_constant_alias(body=statements, statement=statement):
                    alias = self._constant_expression(statement.init)
                    self.constant_aliases[statement.name] = alias
                    for name in self._collect_constant_references(statement.init):
                        include = self.NAMED_CONSTANT_INCLUDES.get(name)
                        if include is not None:
                            self.required_includes.add(include)
                    continue
                if statement.type_name != "unsigned long" and statement.name in self.auto_pin_candidates:
                    following = statements[index + 1] if index + 1 < len(statements) else None
                    if self.can_auto_pin(following_statement=following, statement=statement):
                        self.pinned_register[statement.name] = self.auto_pin_candidates[statement.name]
                        continue
                if statement.name in self.virtual_long_locals:
                    continue
                size = self.TYPE_SIZES[statement.type_name]
                self.allocate_local(statement.name, size=size)
                # Skip the init store for top-level main locals with an Int(0)
                # initializer: the `dw 0` declaration already zeros the cell,
                # and main re-runs from a fresh image each exec.
                if top_level and self.elide_frame and isinstance(statement.init, Int) and statement.init.value == 0 and size == 2:
                    self.zero_init_skippable.add(statement.name)
            elif isinstance(statement, ArrayDecl):
                self.variable_types[statement.name] = statement.type_name
                self.variable_arrays.add(statement.name)
                self.allocate_local(statement.name)
            elif isinstance(statement, If):
                self.scan_locals(statement.body, top_level=False)
                if statement.else_body is not None:
                    self.scan_locals(statement.else_body, top_level=False)
            elif isinstance(statement, (DoWhile, While)):
                self.scan_locals(statement.body, top_level=False)

    def validate_comparison_types(self, left: Node, right: Node, /) -> None:
        r"""Ensure ``==``/``!=``/``<``/``<=``/``>``/``>=`` operand types match.

        Pointers may only be compared to other pointers or ``NULL``;
        ``NULL`` may only appear opposite a pointer; ``char`` values
        must be compared against other ``char`` values or character
        literals (so ``c != 0`` and ``c < 32`` are rejected — use
        ``c != '\0'`` and ``c < ' '``).  Comparing a pointer to a
        non-``NULL`` integer (``if (p == 0)``) is a common C bug, so
        the compiler requires the explicit ``NULL`` spelling.
        """
        left_type = self._type_of_operand(left)
        right_type = self._type_of_operand(right)
        line = left.line or right.line
        if left_type == "pointer" and right_type not in ("pointer", "null"):
            message = f"pointer compared to non-pointer: {left} vs {right}"
            raise CompileError(message, line=line)
        if right_type == "pointer" and left_type not in ("pointer", "null"):
            message = f"pointer compared to non-pointer: {left} vs {right}"
            raise CompileError(message, line=line)
        if left_type == "null" and right_type not in ("pointer", "null"):
            message = f"NULL compared to non-pointer: {left} vs {right}"
            raise CompileError(message, line=line)
        if right_type == "null" and left_type not in ("pointer", "null"):
            message = f"NULL compared to non-pointer: {left} vs {right}"
            raise CompileError(message, line=line)
        if left_type == "char" and right_type != "char":
            message = f"char compared to non-char: {left} vs {right}"
            raise CompileError(message, line=line)
        if right_type == "char" and left_type != "char":
            message = f"char compared to non-char: {left} vs {right}"
            raise CompileError(message, line=line)


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
            CompileError: If the token kind does not match the expected kind.

        """
        token = self.tokens[self.position]
        if kind is not None and token[0] != kind:
            message = f"expected {kind}, got {token[0]} ({token[1]!r})"
            raise CompileError(message, line=token[2])
        self.position += 1
        return token

    @staticmethod
    def fold_binop(operator: str, left: Node, right: Node, /) -> Node:
        """Return a folded node when operands (or a left-subtree tail) are constant.

        Handles two shapes:

        1. ``Int op Int`` collapses to a single ``Int`` — lets
           ``COLUMNS - 1`` become ``39`` at parse time.
        2. ``(X op1 Int1) op2 Int2`` with ``op1, op2`` both additive
           folds the trailing constants through so
           ``(column + 40) - 1`` becomes ``column + 39`` and
           ``(column + 1) % 40`` keeps the ``%`` outer but the inner
           addition is already a tight pair.
        """
        line = left.line
        if isinstance(left, Int) and isinstance(right, Int):
            a, b = left.value, right.value
            if operator == "+":
                return Int(a + b, line=line)
            if operator == "-":
                return Int(a - b, line=line)
            if operator == "*":
                return Int(a * b, line=line)
            if operator == "&":
                return Int(a & b, line=line)
            if operator == "|":
                return Int(a | b, line=line)
            if operator == "^":
                return Int(a ^ b, line=line)
            if operator == "/" and b != 0:
                return Int(a // b, line=line)
            if operator == "%" and b != 0:
                return Int(a % b, line=line)
            if operator == "<<":
                return Int(((a & 0xFFFF) << (b & 0x1F)) & 0xFFFF, line=line)
            if operator == ">>":
                return Int((a & 0xFFFF) >> (b & 0x1F), line=line)
        # Rewrite `x / 2^N` as `x >> N` — a single shr replaces a ~10-byte
        # div sequence and avoids the slow div instruction.  Only kicks
        # in when N is a positive power of two; other divisions stay as-is.
        if operator == "/" and isinstance(right, Int) and right.value > 0 and (right.value & (right.value - 1)) == 0:
            shift = right.value.bit_length() - 1
            return BinOp(">>", left, Int(shift, line=line), line=line)
        if (
            operator in ("+", "-")
            and isinstance(right, Int)
            and isinstance(left, BinOp)
            and left.op in ("+", "-")
            and isinstance(left.right, Int)
        ):
            inner_sign = 1 if left.op == "+" else -1
            outer_sign = 1 if operator == "+" else -1
            combined = inner_sign * left.right.value + outer_sign * right.value
            if combined >= 0:
                return BinOp("+", left.left, Int(combined, line=line), line=line)
            return BinOp("-", left.left, Int(-combined, line=line), line=line)
        return BinOp(operator, left, right, line=line)

    def parse_additive(self) -> Node:
        """Parse an additive expression (addition and subtraction).

        Returns:
            An AST node for the additive expression.

        """
        node = self.parse_multiplicative()
        while self.peek()[0] in ADDITIVE_OPERATORS:
            operator_token = self.eat()
            right = self.parse_multiplicative()
            node = self.fold_binop(operator_token[1], node, right)
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
        line = self.peek()[2]
        self.eat("LBRACE")
        elems = [self.parse_expression()]
        while self.peek()[0] == "COMMA":
            self.eat("COMMA")
            elems.append(self.parse_expression())
        self.eat("RBRACE")
        return ArrayInit(elems, line=line)

    def parse_assignment(self) -> Node:
        """Parse a simple assignment statement.

        Returns:
            An AST node for the assignment.

        """
        token = self.eat("IDENT")
        name = token[1]
        self.eat("ASSIGN")
        expression = self.parse_expression()
        self.eat("SEMI")
        return Assign(name, expression, line=token[2])

    def parse_bitwise_and(self) -> Node:
        """Parse a left-associative bitwise ``&`` expression.

        Returns:
            A ``BinOp`` chain or the underlying comparison.

        """
        left = self.parse_comparison()
        while self.peek()[0] == "AMP":
            self.eat()
            right = self.parse_comparison()
            left = self.fold_binop("&", left, right)
        return left

    def parse_bitwise_or(self) -> Node:
        """Parse a left-associative bitwise ``|`` expression.

        Lower precedence than ``^`` and ``&``, higher than ``&&``.
        """
        left = self.parse_bitwise_xor()
        while self.peek()[0] == "PIPE":
            self.eat()
            right = self.parse_bitwise_xor()
            left = self.fold_binop("|", left, right)
        return left

    def parse_bitwise_xor(self) -> Node:
        """Parse a left-associative bitwise ``^`` expression.

        Lower precedence than ``&``, higher than ``|``.
        """
        left = self.parse_bitwise_and()
        while self.peek()[0] == "CARET":
            self.eat()
            right = self.parse_bitwise_and()
            left = self.fold_binop("^", left, right)
        return left

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
        token = self.eat("IDENT")
        name = token[1]
        self.eat("LPAREN")
        arguments = self.parse_arguments()
        self.eat("SEMI")
        return Call(name, arguments, line=token[2])

    def parse_comparison(self) -> Node:
        """Parse a comparison expression.

        Returns:
            An AST node for the comparison expression.

        """
        left = self.parse_shift()
        if self.peek()[0] in COMPARISON_OPERATORS:
            operator_token = self.eat()
            right = self.parse_shift()
            return BinOp(operator_token[1], left, right, line=operator_token[2])
        return left

    def parse_compound_assignment(self) -> Node:
        """Parse a compound assignment (``+=``, ``&=``, ``|=``, ``^=``, ``<<=``, ``>>=``).

        Returns:
            An AST node for the desugared assignment ``x = x op rhs``.

        """
        token = self.eat("IDENT")
        name = token[1]
        line = token[2]
        op_token = self.eat()
        operator = COMPOUND_ASSIGN_OPERATORS[op_token[0]]
        expression = self.parse_expression()
        self.eat("SEMI")
        return Assign(name, BinOp(operator, Var(name, line=line), expression, line=line), line=line)

    def parse_condition(self) -> Node:
        """Parse an if/while condition.

        Wraps a bare expression as ``expr != 0`` so that ``if (error)``
        is equivalent to ``if (error != 0)``.  Comparisons at the top
        level are returned unchanged.

        Returns:
            A BinOp AST node suitable for conditional jumps.

        """
        expression = self.parse_expression()
        if isinstance(expression, (LogicalAnd, LogicalOr)):
            return expression
        if isinstance(expression, BinOp) and expression.op in JUMP_WHEN_FALSE:
            return expression
        return BinOp("!=", expression, Int(0, line=expression.line), line=expression.line)

    def parse_do_while(self) -> Node:
        """Parse a do...while loop statement.

        Returns:
            A ``DoWhile`` AST node.

        """
        token = self.eat("DO")
        self.eat("LBRACE")
        body = self.parse_block()
        self.eat("WHILE")
        self.eat("LPAREN")
        condition = self.parse_condition()
        self.eat("RPAREN")
        self.eat("SEMI")
        return DoWhile(condition, body, line=token[2])

    def parse_expression(self) -> Node:
        """Parse an expression.

        Returns:
            An AST node for the expression.

        """
        return self.parse_logical_or()

    def parse_if(self) -> Node:
        """Parse an if statement.

        Returns:
            An AST node for the if statement.

        """
        token = self.eat("IF")
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
        return If(condition, body, else_body, line=token[2])

    def parse_index_assignment(self) -> Node:
        """Parse an indexed assignment ``name[index] = expr;``."""
        token = self.eat("IDENT")
        name = token[1]
        self.eat("LBRACKET")
        index = self.parse_expression()
        self.eat("RBRACKET")
        self.eat("ASSIGN")
        expr = self.parse_expression()
        self.eat("SEMI")
        return IndexAssign(name, index, expr, line=token[2])

    def parse_logical_and(self) -> Node:
        """Parse a left-associative ``&&`` expression.

        Returns:
            A ``LogicalAnd`` tree or the underlying bitwise-OR node.

        """
        left = self.parse_bitwise_or()
        while self.peek()[0] == "AND_AND":
            op_token = self.eat()
            right = self.parse_bitwise_or()
            left = LogicalAnd(left, right, line=op_token[2])
        return left

    def parse_logical_or(self) -> Node:
        """Parse a left-associative ``||`` expression.

        Returns:
            A ``LogicalOr`` tree or the underlying ``&&`` node.

        """
        left = self.parse_logical_and()
        while self.peek()[0] == "OR_OR":
            op_token = self.eat()
            right = self.parse_logical_and()
            left = LogicalOr(left, right, line=op_token[2])
        return left

    def parse_multiplicative(self) -> Node:
        """Parse a multiplicative expression (multiplication and division).

        Returns:
            An AST node for the multiplicative expression.

        """
        node = self.parse_primary()
        while self.peek()[0] in MULTIPLICATIVE_OPERATORS:
            operator_token = self.eat()
            right = self.parse_primary()
            node = self.fold_binop(operator_token[1], node, right)
        return node

    def parse_shift(self) -> Node:
        """Parse a shift expression (``<<`` and ``>>``).

        Higher precedence than comparison, lower than additive — matches
        C's precedence order.
        """
        node = self.parse_additive()
        while self.peek()[0] in SHIFT_OPERATORS:
            operator_token = self.eat()
            right = self.parse_additive()
            node = self.fold_binop(operator_token[1], node, right)
        return node

    def _parse_attribute(self, *, line: int) -> tuple[str, object]:
        """Consume a single ``__attribute__((name(args)))`` directive.

        Returns a ``(name, value)`` tuple that the caller dispatches
        on.  Supported kinds:

        * ``("regparm", 1)`` — first arg arrives in AX (fastcall).
        * ``("asm_register", "si")`` — file-scope global aliases SI.
        * ``("carry_return", True)`` — int return is reported via CF
          (CF clear = 1/true/success, CF set = 0/false/failure); no
          parenthesised argument list.
        * ``("always_inline", True)`` — inline the single-asm-body
          function at every C-level call site; no free-standing body.

        clang silently accepts regparm on x86 targets; asm_register /
        carry_return are unknown to clang and produce a
        ``-Wunknown-attributes`` warning (returncode stays 0), so the
        syntax survives ``test_cc.py``.
        """
        self.eat("IDENT")  # __attribute__
        self.eat("LPAREN")
        self.eat("LPAREN")
        attr_name_token = self.eat("IDENT")
        attr_name = attr_name_token[1]
        if attr_name == "regparm":
            self.eat("LPAREN")
            count_token = self.eat("NUMBER")
            self.eat("RPAREN")
            self.eat("RPAREN")
            self.eat("RPAREN")
            count = int(count_token[1])
            if count != 1:
                message = f"regparm({count}) not supported; only regparm(1) is implemented"
                raise CompileError(message, line=line)
            return ("regparm", count)
        if attr_name == "asm_register":
            self.eat("LPAREN")
            reg_token = self.eat("STRING")
            self.eat("RPAREN")
            self.eat("RPAREN")
            self.eat("RPAREN")
            reg_name = reg_token[1][1:-1]
            if reg_name != "si":
                message = f"asm_register('{reg_name}') not supported; only 'si' is implemented"
                raise CompileError(message, line=line)
            return ("asm_register", reg_name)
        if attr_name == "carry_return":
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("carry_return", True)
        if attr_name == "always_inline":
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("always_inline", True)
        message = f"unsupported attribute '{attr_name}'"
        raise CompileError(message, line=line)

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
            CompileError: If an unexpected token is encountered.

        """
        token = self.peek()
        line = token[2]
        if token[0] == "SIZEOF":
            return self.parse_sizeof()
        if token[0] == "NUMBER":
            self.eat()
            return Int(int(token[1], 0), line=line)
        if token[0] == "CHAR_LIT":
            self.eat()
            return Char(decode_first_character(token[1][1:-1], line=line), line=line)
        if token[0] == "STRING":
            self.eat()
            content = token[1][1:-1]
            # Adjacent string literals concatenate — standard C behavior.
            # ``"foo" "bar"`` folds to ``"foobar"`` at parse time.
            while self.peek()[0] == "STRING":
                content += self.eat()[1][1:-1]
            return String(content, line=line)
        if token[0] == "IDENT":
            self.eat()
            if self.peek()[0] == "LPAREN":
                self.eat("LPAREN")
                return Call(token[1], self.parse_arguments(), line=line)
            if self.peek()[0] == "LBRACKET":
                self.eat("LBRACKET")
                index = self.parse_expression()
                self.eat("RBRACKET")
                return Index(token[1], index, line=line)
            return Var(token[1], line=line)
        if token[0] == "NOT":
            self.eat()
            return BinOp("==", self.parse_primary(), Int(0, line=line), line=line)
        if token[0] == "TILDE":
            self.eat()
            operand = self.parse_primary()
            if isinstance(operand, Int):
                return Int(operand.value ^ 0xFFFF, line=line)
            return BinOp("^", operand, Int(0xFFFF, line=line), line=line)
        if token[0] == "MINUS":
            self.eat()
            operand = self.parse_primary()
            # Fold ``-<int>`` to a single negative ``Int`` so ``-1`` and ``-42``
            # round-trip as literals instead of an addition node.  Runtime
            # negation still rewrites to ``0 - x`` to reuse the subtract path.
            if isinstance(operand, Int):
                return Int(-operand.value, line=line)
            return BinOp("-", Int(0, line=line), operand, line=line)
        if token[0] == "LPAREN":
            self.eat()
            expression = self.parse_expression()
            self.eat("RPAREN")
            return expression
        message = f"expected expression, got {token[0]} ({token[1]!r})"
        raise CompileError(message, line=line)

    def parse_program(self) -> Node:
        """Parse the entire program as a sequence of top-level declarations.

        Top-level declarations are either function definitions or
        file-scope (``global``) variable / array declarations.  Both
        start with ``type IDENT``; the token after the name
        disambiguates — ``(`` introduces a function, anything else a
        variable.
        """
        line = self.peek()[2]
        functions: list[Node] = []
        globals_list: list[Node] = []
        while self.peek()[0] != "EOF":
            declaration = self.parse_top_level_declaration()
            if declaration is None:
                # Function prototype — swallowed by parse_top_level_declaration
                # for clang's benefit; cc.py doesn't need it in the AST.
                continue
            if isinstance(declaration, Function):
                functions.append(declaration)
            else:
                globals_list.append(declaration)
        return Program(functions, globals=globals_list, line=line)

    def parse_sizeof(self) -> Node:
        """Parse a sizeof expression.

        Returns:
            An AST node for sizeof(type) or sizeof(variable).

        """
        token = self.eat("SIZEOF")
        self.eat("LPAREN")
        # sizeof(type) or sizeof(variable)
        if self.peek()[0] in TYPE_TOKENS:
            type_string = self.parse_type()
            self.eat("RPAREN")
            return SizeofType(type_string, line=token[2])
        name = self.eat("IDENT")[1]
        self.eat("RPAREN")
        return SizeofVar(name, line=token[2])

    def parse_statement(self) -> Node:
        """Parse a single statement.

        Returns:
            An AST node for the statement.

        Raises:
            CompileError: If an unexpected token is encountered.

        """
        token = self.peek()
        if token[0] in TYPE_TOKENS:
            return self.parse_variable_declaration()
        if token[0] == "IF":
            return self.parse_if()
        if token[0] == "BREAK":
            self.eat("BREAK")
            self.eat("SEMI")
            return Break(line=token[2])
        if token[0] == "CONTINUE":
            self.eat("CONTINUE")
            self.eat("SEMI")
            return Continue(line=token[2])
        if token[0] == "DO":
            return self.parse_do_while()
        if token[0] == "RETURN":
            self.eat("RETURN")
            value = None
            if self.peek()[0] != "SEMI":
                value = self.parse_expression()
            self.eat("SEMI")
            return Return(value, line=token[2])
        if token[0] == "WHILE":
            return self.parse_while()
        if token[0] == "IDENT":
            next_kind = self.peek(offset=1)[0]
            if next_kind == "ASSIGN":
                return self.parse_assignment()
            if next_kind in COMPOUND_ASSIGN_OPERATORS:
                return self.parse_compound_assignment()
            if next_kind == "LBRACKET":
                return self.parse_index_assignment()
            return self.parse_call_statement()
        message = f"expected statement, got {token[0]} ({token[1]!r})"
        raise CompileError(message, line=token[2])

    def parse_top_level_declaration(self) -> Node:
        """Parse a function definition, a file-scope variable / array, or a file-scope ``asm(...)``.

        Dispatches on the token after ``type IDENT``: ``(`` drives the
        function path, any other token means a global declaration.  A
        bare ``asm("...");`` at the top level is emitted verbatim into
        the output's data tail — useful for raw tables and labels.
        """
        line = self.peek()[2]
        if self.peek()[0] == "IDENT" and self.peek()[1] == "asm" and self.peek(offset=1)[0] == "LPAREN":
            self.eat("IDENT")
            self.eat("LPAREN")
            string_token = self.eat("STRING")
            content = string_token[1][1:-1]
            # Adjacent string literals concatenate (as in parse_primary).
            while self.peek()[0] == "STRING":
                content += self.eat()[1][1:-1]
            self.eat("RPAREN")
            self.eat("SEMI")
            return InlineAsm(content, line=line)
        # Optional leading ``__attribute__((...))`` directives.
        # ``regparm(1)`` applies to function definitions (arg 0 in AX);
        # ``asm_register("REG")`` applies to file-scope VarDecls (the
        # variable aliases the named CPU register).  Both may appear
        # before the return type.  ``regparm`` may also appear after
        # the function parameter list; ``asm_register`` is leading-only.
        regparm_count = 0
        asm_register: str | None = None
        carry_return = False
        always_inline = False
        while self.peek()[0] == "IDENT" and self.peek()[1] == "__attribute__":
            kind, value = self._parse_attribute(line=line)
            if kind == "regparm":
                regparm_count = value
            elif kind == "carry_return":
                carry_return = True
            elif kind == "always_inline":
                always_inline = True
            else:
                asm_register = value
        type_string = self.parse_type()
        name_token = self.eat("IDENT")
        name = name_token[1]
        if self.peek()[0] == "LPAREN":
            if asm_register is not None:
                message = "asm_register attribute is not valid on function definitions"
                raise CompileError(message, line=line)
            self.eat("LPAREN")
            parameters = self.parse_parameters()
            self.eat("RPAREN")
            while self.peek()[0] == "IDENT" and self.peek()[1] == "__attribute__":
                kind, value = self._parse_attribute(line=line)
                if kind == "regparm":
                    if regparm_count != 0:
                        message = "regparm attribute specified twice"
                        raise CompileError(message, line=line)
                    regparm_count = value
                elif kind == "carry_return":
                    carry_return = True
                elif kind == "always_inline":
                    always_inline = True
                else:
                    message = f"trailing {kind} attribute is not valid on function definitions"
                    raise CompileError(message, line=line)
            if regparm_count > 0 and not parameters:
                message = "regparm(1) requires at least one parameter"
                raise CompileError(message, line=line)
            if carry_return and len(parameters) > regparm_count:
                # Stack-passed args would require an ``add sp, N`` cleanup
                # after the call, which clobbers CF.  carry_return callees
                # must arrive via AX only (regparm(1)) or take no args.
                message = "carry_return functions may not take stack args; use 0 params or regparm(1)"
                raise CompileError(message, line=line)
            if always_inline and len(parameters) > regparm_count:
                # Inlining splices the body in place; stack args would
                # need a caller-side cleanup that doesn't exist.
                message = "always_inline functions may not take stack args; use 0 params or regparm(1)"
                raise CompileError(message, line=line)
            if self.peek()[0] == "SEMI":
                # Function prototype (no body).  cc.py's two-pass
                # function-name resolution doesn't need prototypes, but
                # clang's ISO C99 declare-before-use rule requires them
                # when a pure-C caller references a function defined
                # later in the same translation unit.  Parsed and
                # swallowed here; ``parse_program`` drops the None
                # return so nothing lands in the AST.
                self.eat("SEMI")
                return None
            self.eat("LBRACE")
            return Function(
                name,
                parameters,
                self.parse_block(),
                line=line,
                regparm_count=regparm_count,
                carry_return=carry_return,
                always_inline=always_inline,
            )
        if regparm_count != 0:
            message = "regparm attribute is not valid on global variables"
            raise CompileError(message, line=line)
        if carry_return:
            message = "carry_return attribute is not valid on global variables"
            raise CompileError(message, line=line)
        if always_inline:
            message = "always_inline attribute is not valid on global variables"
            raise CompileError(message, line=line)
        # File-scope variable: scalar or array.  Globals may specify a
        # size inside ``[...]`` (unlike locals) since there is no
        # runtime initializer to imply one.
        is_array = False
        size_expression: Node | None = None
        if self.peek()[0] == "LBRACKET":
            self.eat("LBRACKET")
            is_array = True
            if self.peek()[0] != "RBRACKET":
                size_expression = self.parse_expression()
            self.eat("RBRACKET")
        init: Node | None = None
        if self.peek()[0] == "ASSIGN":
            self.eat("ASSIGN")
            init = self.parse_array_init() if is_array else self.parse_expression()
        self.eat("SEMI")
        if is_array:
            if asm_register is not None:
                message = "asm_register attribute is not valid on arrays"
                raise CompileError(message, line=line)
            if size_expression is None and init is None:
                message = f"global array '{name}' needs either a size or an initializer"
                raise CompileError(message, line=line)
            return ArrayDecl(name, type_string, init, line=line, size=size_expression)
        return VarDecl(name, type_string, init, line=line, asm_register=asm_register)

    def parse_type(self) -> str:
        """Parse a type specifier (void, int, char, char*, unsigned long).

        An optional leading ``const`` is accepted and discarded — the C
        subset has no notion of const-ness but tolerating the keyword
        lets sources carry POSIX-compatible signatures (e.g. ``int
        strcmp(const char *, const char *)``) that ``<string.h>``
        expects when the same source is syntax-checked by clang.

        Returns:
            The type as a string.

        Raises:
            CompileError: If an unexpected token is encountered, or a bare
                ``long`` / ``unsigned`` without ``long`` appears.

        """
        if self.peek()[0] == "CONST":
            self.eat()
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
                message = f"expected 'long' after 'unsigned', got {following[1]!r}"
                raise CompileError(message, line=token[2])
            self.eat()
            return "unsigned long"
        if token[0] == "LONG":
            message = "bare 'long' is not supported; use 'unsigned long'"
            raise CompileError(message, line=token[2])
        message = f"expected type, got {token[0]} ({token[1]!r})"
        raise CompileError(message, line=token[2])

    def parse_variable_declaration(self) -> Node:
        """Parse a variable or array declaration.

        Returns:
            An AST node for the declaration.

        """
        line = self.peek()[2]
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
            return ArrayDecl(name, type_string, init, line=line)
        return VarDecl(name, type_string, init, line=line)

    def parse_while(self) -> Node:
        """Parse a while loop statement.

        Returns:
            An AST node for the while loop.

        """
        token = self.eat("WHILE")
        self.eat("LPAREN")
        condition = self.parse_condition()
        self.eat("RPAREN")
        self.eat("LBRACE")
        return While(condition, self.parse_block(), line=token[2])

    def peek(self, offset: int = 0) -> tuple[str, str, int]:
        """Return the token at the current position plus an optional offset.

        Returns:
            The token as a (kind, text, line) triple.

        """
        return self.tokens[self.position + offset]


def apply_defines(
    *,
    defines: dict[str, str],
    tokens: list[tuple[str, str, int]],
) -> list[tuple[str, str, int]]:
    """Substitute IDENT tokens matching a ``#define`` with the macro's tokens.

    Each macro value is retokenized on every occurrence so the caller
    can use any token sequence (numbers, char literals, parenthesized
    expressions).  The substituted tokens inherit the line number of
    the original IDENT so diagnostics point at the use site, not the
    define site.  No recursive expansion or function-like macros.
    """
    if not defines:
        return tokens
    result: list[tuple[str, str, int]] = []
    for kind, text, line in tokens:
        if kind == "IDENT" and text in defines:
            value_tokens = tokenize(defines[text])
            for value_kind, value_text, _ in value_tokens[:-1]:  # drop trailing EOF
                result.append((value_kind, value_text, line))
        else:
            result.append((kind, text, line))
    return result


def _decode_string_escapes(text: str, /) -> str:
    r"""Decode every C escape sequence in *text* to its literal character.

    Handles ``\n``/``\t``/``\r``/``\b``/``\0``/``\\``/``\"`` from
    :data:`CHARACTER_ESCAPES` plus ``\xNN`` hex escapes.  Unknown
    single-letter escapes are passed through unchanged — the NASM
    output is the consumer, and treating them as literal escape
    sequences for the downstream assembler keeps callers from having
    to double-escape assembler-visible backslashes.
    """
    result: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "x" and i + 3 < len(text):
                result.append(chr(int(text[i + 2 : i + 4], 16)))
                i += 4
                continue
            if nxt in CHARACTER_ESCAPES:
                result.append(chr(CHARACTER_ESCAPES[nxt]))
                i += 2
                continue
        result.append(text[i])
        i += 1
    return "".join(result)


def decode_first_character(text: str, /, *, line: int | None = None) -> int:
    """Return the byte value of the first character in a C string literal.

    Returns:
        The integer byte value of the decoded character.

    Raises:
        CompileError: If the text contains an unrecognized escape sequence.

    """
    if text[0] == "\\" and len(text) >= 2:
        if text[1] == "x" and len(text) >= 3:
            return int(text[2:], 16)  # noqa: FURB166
        if text[1] not in CHARACTER_ESCAPES:
            message = f"unknown escape sequence: '\\{text[1]}'"
            raise CompileError(message, line=line)
        return CHARACTER_ESCAPES[text[1]]
    return ord(text[0])


def main() -> int:
    """Compile a C source file to NASM assembly.

    Returns:
        Exit code (0 for success, 1 for usage or compilation error).

    """
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("Usage: cc.py <input.c> [output.asm]", file=sys.stderr)
        return 1

    input_path = sys.argv[1]
    try:
        source = Path(input_path).read_text(encoding="utf-8")
        source, defines = preprocess(source, include_base=Path(input_path).parent)
        tokens = tokenize(source)
        tokens = apply_defines(defines=defines, tokens=tokens)
        ast = Parser(tokens).parse_program()
        output = CodeGenerator(defines=defines).generate(ast)
    except CompileError as error:
        location = f"{input_path}:{error.line}" if error.line else input_path
        print(f"{location}: error: {error.message}", file=sys.stderr)
        return 1

    if len(sys.argv) == 3:
        Path(sys.argv[2]).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


INCLUDE_PATTERN = re.compile(r'\s*"([^"]+)"\s*$')


def preprocess(
    source: str,
    /,
    *,
    include_base: Path | None = None,
    include_stack: frozenset[Path] = frozenset(),
) -> tuple[str, dict[str, str]]:
    """Expand ``#include "..."`` directives and collect ``#define`` macros.

    Only object-like ``#define NAME VALUE`` macros are supported.  Each
    ``#define`` line is replaced with a blank line so downstream line
    numbers stay correct.

    ``#include`` accepts only the double-quoted form and resolves the
    path relative to *include_base* (the directory of the source
    currently being preprocessed) — matching NASM's ``%include``.
    Included files are preprocessed recursively; their ``#define``
    entries merge into the outer pool so later definitions override.
    ``include_stack`` carries the set of files currently being
    expanded so a cycle is rejected with a clear error.  The directive
    line itself is replaced by the included file's processed text, so
    error line numbers after an include shift by the included file's
    length — acceptable in the absence of ``#line`` support.

    Returns:
        (processed_source, defines).  ``defines`` maps each macro name
        to the raw value text, which is retokenized at substitution
        time so the tokens inherit the current position's line number.

    """
    if include_base is None:
        include_base = Path()
    defines: dict[str, str] = {}
    output_lines: list[str] = []
    for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#include"):
            match = INCLUDE_PATTERN.match(stripped[len("#include") :])
            if match is None:
                message = f"malformed #include: {line.rstrip()!r}"
                raise CompileError(message, line=line_number)
            include_path = (include_base / match.group(1)).resolve()
            if include_path in include_stack:
                message = f"circular #include of {include_path}"
                raise CompileError(message, line=line_number)
            try:
                included_source = include_path.read_text(encoding="utf-8")
            except OSError as error:
                message = f"cannot open #include file {match.group(1)!r}: {error}"
                raise CompileError(message, line=line_number) from error
            included_text, included_defines = preprocess(
                included_source,
                include_base=include_path.parent,
                include_stack=include_stack | {include_path},
            )
            defines.update(included_defines)
            output_lines.append(included_text)
            continue
        if not stripped.startswith("#define"):
            output_lines.append(line)
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 3:
            message = f"malformed #define: {line.rstrip()!r}"
            raise CompileError(message, line=line_number)
        name = parts[1]
        value = parts[2].rstrip()
        if not value:
            message = f"empty #define value for {name!r}"
            raise CompileError(message, line=line_number)
        defines[name] = value
        output_lines.append("\n")  # Preserve line numbering.
    return "".join(output_lines), defines


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


def tokenize(source: str, /) -> list[tuple[str, str, int]]:
    """Tokenize C source code into a list of (kind, text, line) triples.

    Returns:
        A list of (kind, text, line) token triples.

    Raises:
        CompileError: If an unexpected character is encountered.

    """
    tokens: list[tuple[str, str, int]] = []
    position = 0
    line = 1
    while position < len(source):
        match = TOKEN_PATTERN.match(source, position)
        if not match:
            message = f"unexpected character {source[position]!r}"
            raise CompileError(message, line=line)
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
