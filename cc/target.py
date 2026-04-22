"""Code-generation target abstraction.

``CodegenTarget`` captures every mode-dependent choice the code
generator needs — register names, integer width, syscall ABI — so
``X86CodeGenerator`` can route calls through one object rather than
branching on ``bits == 16`` everywhere.  Adding a new target (new ISA,
different ABI) means subclassing ``CodegenTarget`` and passing an
instance to ``X86CodeGenerator``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

EREG_LOW_WORD: dict[str, str] = {
    "eax": "ax",
    "ecx": "cx",
    "edx": "dx",
    "ebx": "bx",
    "esi": "si",
    "edi": "di",
    "ebp": "bp",
    "esp": "sp",
}

#: Inverse of :data:`EREG_LOW_WORD`: widen a 16-bit GP name to its
#: 32-bit E-register.  Used by :meth:`X86CodegenTarget32.widen_gp` to
#: promote user-supplied ``asm_register("si")`` aliases and other
#: stored 16-bit names when the target is 32-bit flat pmode.
LOW_WORD_EREG: dict[str, str] = {v: k for k, v in EREG_LOW_WORD.items()}


class CodegenTarget(ABC):
    """Architecture-independent interface for code-generation targets.

    ``X86CodeGenerator`` holds exactly one ``CodegenTarget`` and routes
    every mode-dependent choice through it.  To add a new target
    (different ISA, new ABI, …) subclass this and pass an instance
    to ``X86CodeGenerator``.
    """

    #: Accumulator register name (e.g. ``"ax"`` / ``"eax"``).
    acc: str
    #: Counter / shift register name (``"cx"`` / ``"ecx"``).
    count_register: str
    #: Base-pointer register name.
    base_register: str
    #: Stack-pointer register name.
    stack_register: str
    #: NASM size keyword for the native integer width (``"word"`` / ``"dword"``).
    word_size: str
    #: ``sizeof(int)`` in bytes.
    int_size: int
    #: ``[bp+N]`` offset to the first stack parameter.
    param_slot_base: int
    #: ``sizeof`` table for all supported C types.
    type_sizes: ClassVar[dict[str, int]]
    #: Caller-save scratch registers available for local pinning.
    register_pool: ClassVar[tuple[str, ...]]
    #: Invocation sequence per syscall name.
    syscall_sequences: ClassVar[dict[str, tuple[str, ...]]]
    #: General-purpose registers that are not the accumulator.
    non_acc_registers: ClassVar[frozenset[str]]

    @staticmethod
    @abstractmethod
    def preamble_lines() -> list[str]:
        """NASM directives emitted before ``org``."""

    @staticmethod
    @abstractmethod
    def far_ref(base_reg: str) -> str:
        """Memory-operand string for ``far_read*/far_write*`` builtins."""

    @staticmethod
    def low_word(reg: str) -> str:
        """Return the low-word alias of *reg*, or *reg* unchanged.

        Default is the identity — correct for ISAs with no sub-register
        aliasing.  x86 overrides this to map ``eax`` → ``ax``, etc.
        """
        return reg

    @staticmethod
    def widen_gp(reg: str) -> str:
        """Return the target-width GP register name for a 16-bit alias.

        Default is the identity — correct for 16-bit x86 (``"si"`` →
        ``"si"``) and for any non-x86 ISA that doesn't partition GP
        names by width.  32-bit x86 overrides to map ``"si"`` →
        ``"esi"`` so stored 16-bit names (``asm_register("si")``,
        pinned register values, cached ``[bp+N]`` indexes) read at
        the target's native width wherever they're emitted.
        """
        return reg


class X86CodegenTarget(CodegenTarget):
    """Shared state for all x86 BBoeOS targets.

    Both the 16-bit real-mode and 32-bit flat-pmode targets use the
    same BBoeOS ``int 30h`` syscall ABI and x86 E-register aliasing.
    The ``bx`` / ``dx`` / ``si`` / ``di`` register fields live on this
    class (not ``CodegenTarget``) because they name physical x86
    registers; a future non-x86 backend would subclass
    ``CodegenTarget`` directly and expose its own role-shaped
    register names instead.
    """

    #: Base / general-purpose register (``"bx"`` / ``"ebx"``).  Holds
    #: the BBoeOS syscall fd argument and serves as a general
    #: pointer scratch for indexed-memory addressing.
    bx_register: str
    #: Destination-index register (``"di"`` / ``"edi"``).  Loaded
    #: with destination-pointer arguments to string-op / syscall
    #: builtins (``memcpy``, ``read``, ``recvfrom``, ``mac``, …).
    di_register: str
    #: Data register (``"dx"`` / ``"edx"``).  Half of the ``mul`` /
    #: ``div`` result pair (DX:AX / EDX:EAX) and the UDP ``sendto`` /
    #: ``recvfrom`` port / remainder argument register.
    dx_register: str
    #: Source-index register (``"si"`` / ``"esi"``).  Loaded with
    #: source-pointer arguments to string-op / syscall builtins
    #: (``mov``, ``die``, ``exec``, ``write``, ``sendto``, …).
    si_register: str

    #: BBoeOS kernel ABI: every syscall uses ``int 30h``.
    SYSCALL_SEQUENCES: ClassVar[dict[str, tuple[str, ...]]] = {
        "EXEC": ("mov ah, SYS_EXEC", "int 30h"),
        "FS_CHMOD": ("mov ah, SYS_FS_CHMOD", "int 30h"),
        "FS_MKDIR": ("mov ah, SYS_FS_MKDIR", "int 30h"),
        "FS_RENAME": ("mov ah, SYS_FS_RENAME", "int 30h"),
        "FS_UNLINK": ("mov ah, SYS_FS_UNLINK", "int 30h"),
        "IO_CLOSE": ("mov ah, SYS_IO_CLOSE", "int 30h"),
        "IO_FSTAT": ("mov ah, SYS_IO_FSTAT", "int 30h"),
        "IO_OPEN": ("mov ah, SYS_IO_OPEN", "int 30h"),
        "IO_READ": ("mov ah, SYS_IO_READ", "int 30h"),
        "IO_WRITE": ("mov ah, SYS_IO_WRITE", "int 30h"),
        "NET_MAC": ("mov ah, SYS_NET_MAC", "int 30h"),
        "NET_OPEN": ("mov ah, SYS_NET_OPEN", "int 30h"),
        "NET_RECVFROM": ("mov ah, SYS_NET_RECVFROM", "int 30h"),
        "NET_SENDTO": ("mov ah, SYS_NET_SENDTO", "int 30h"),
        "REBOOT": ("mov ah, SYS_REBOOT", "int 30h"),
        "RTC_DATETIME": ("mov ah, SYS_RTC_DATETIME", "int 30h"),
        "RTC_SLEEP": ("mov ah, SYS_RTC_SLEEP", "int 30h"),
        "RTC_UPTIME": ("mov ah, SYS_RTC_UPTIME", "int 30h"),
        "SHUTDOWN": ("mov ah, SYS_SHUTDOWN", "int 30h"),
        "VIDEO_MODE": ("mov ah, SYS_VIDEO_MODE", "int 30h"),
    }
    syscall_sequences = SYSCALL_SEQUENCES

    @staticmethod
    def low_word(reg: str) -> str:
        """Return the 16-bit low-word alias of *reg*, or *reg* unchanged."""
        return EREG_LOW_WORD.get(reg, reg)


class X86CodegenTarget16(X86CodegenTarget):
    """16-bit real-mode x86 target (BBoeOS stage 2 and user programs)."""

    acc = "ax"
    base_register = "bp"
    bx_register = "bx"
    count_register = "cx"
    di_register = "di"
    dx_register = "dx"
    si_register = "si"
    stack_register = "sp"
    word_size = "word"
    int_size = 2
    param_slot_base = 4
    type_sizes: ClassVar[dict[str, int]] = {
        "char": 1,
        "char*": 2,
        "int": 2,
        "uint8_t": 1,
        "uint8_t*": 2,
        "unsigned long": 4,
        "void": 0,
    }
    register_pool: ClassVar[tuple[str, ...]] = ("dx", "cx", "bx", "di")
    non_acc_registers: ClassVar[frozenset[str]] = frozenset({"bx", "cx", "dx", "si", "di", "bp"})

    @staticmethod
    def preamble_lines() -> list[str]:
        """No preamble needed for 16-bit real-mode targets."""
        return []

    @staticmethod
    def far_ref(base_reg: str) -> str:
        """ES-segment override for real-mode far-memory access."""
        return f"[es:{base_reg}]"


class X86CodegenTarget32(X86CodegenTarget):
    """32-bit flat-pmode x86 target (BBoeOS ring-0 protected mode)."""

    acc = "eax"
    base_register = "ebp"
    bx_register = "ebx"
    count_register = "ecx"
    di_register = "edi"
    dx_register = "edx"
    si_register = "esi"
    stack_register = "esp"
    word_size = "dword"
    int_size = 4
    param_slot_base = 8
    type_sizes: ClassVar[dict[str, int]] = {
        "char": 1,
        "char*": 4,
        "int": 4,
        "uint8_t": 1,
        "uint8_t*": 4,
        "unsigned long": 4,
        "void": 0,
    }
    register_pool: ClassVar[tuple[str, ...]] = ("edx", "ecx", "ebx", "edi")
    non_acc_registers: ClassVar[frozenset[str]] = frozenset({"ebx", "ecx", "edx", "esi", "edi", "ebp"})

    @staticmethod
    def preamble_lines() -> list[str]:
        """Emit ``[bits 32]`` to switch NASM to 32-bit encoding."""
        return ["        [bits 32]"]

    @staticmethod
    def far_ref(base_reg: str) -> str:
        """Flat DS covers all memory; no segment override needed."""
        return f"[{base_reg}]"

    @staticmethod
    def widen_gp(reg: str) -> str:
        """Promote a 16-bit GP register name to its E-register form."""
        return LOW_WORD_EREG.get(reg, reg)
