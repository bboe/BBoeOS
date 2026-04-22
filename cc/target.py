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


class CodegenTarget(ABC):
    """Architecture-independent interface for code-generation targets.

    ``X86CodeGenerator`` holds exactly one ``CodegenTarget`` and routes
    every mode-dependent choice through it.  To add a new target
    (different ISA, new ABI, …) subclass this and pass an instance
    to ``X86CodeGenerator``.
    """

    #: Accumulator register name (e.g. ``"ax"`` / ``"eax"``).
    acc: str
    #: Counter / shift register name.
    count_register: str
    #: Data register name.
    dx_register: str
    #: Base-pointer register name.
    bp_register: str
    #: Stack-pointer register name.
    sp_register: str
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


class X86CodegenTarget(CodegenTarget):
    """Shared state for all x86 BBoeOS targets.

    Both the 16-bit real-mode and 32-bit flat-pmode targets use the
    same BBoeOS ``int 30h`` syscall ABI and x86 E-register aliasing.
    """

    #: BBoeOS kernel ABI: every syscall uses ``int 30h``.
    SYSCALL_SEQUENCES: ClassVar[dict[str, tuple[str, ...]]] = {
        "EXEC": ("mov ah, SYS_EXEC", "int 30h"),
        "FS_CHMOD": ("mov ah, SYS_FS_CHMOD", "int 30h"),
        "FS_MKDIR": ("mov ah, SYS_FS_MKDIR", "int 30h"),
        "FS_RENAME": ("mov ah, SYS_FS_RENAME", "int 30h"),
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
    count_register = "cx"
    dx_register = "dx"
    bp_register = "bp"
    sp_register = "sp"
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
    count_register = "ecx"
    dx_register = "edx"
    bp_register = "ebp"
    sp_register = "esp"
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
