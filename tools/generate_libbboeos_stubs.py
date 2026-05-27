#!/usr/bin/env python3
"""Generate user/libbboeos/libbboeos_stubs.S from kernel/include/constants.asm.

Emits a tiny `jmp [FUNCTION_<NAME>_PTR]` thunk per libbboeos C export.
Clang-built userland programs (ports/doom, tests/test_libbboeos_qemu.py)
link this object file BEFORE libbboeos.a so the archive's full bodies
never get pulled in — every call dispatches through the shared
libbboeos blob's pointer table instead of being statically duplicated
per program.

Rule: emit a stub for every FUNCTION_<NAME>_PTR entry whose un-suffixed
FUNCTION_<NAME> counterpart is *absent*.  The legacy 13-entry block at
the top of FUNCTION_POINTER_TABLE (FUNCTION_DIE_PTR, ...) has both
FUNCTION_DIE and FUNCTION_DIE_PTR — those resolve to libbboeos.asm's
shared_* helpers and aren't libbboeos exports, so they're skipped.

Re-run is idempotent — the script writes the file only if the contents
differ, so make / build.py can call it unconditionally without forcing
recompiles.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ASSIGN = re.compile(r"^\s*%assign\s+(?P<name>\w+)\s+(?P<value>.+?)\s*(?:;.*)?$")
PROTOTYPE = re.compile(
    r"^[\w\s\*]+?\b(\w+)\s*\(([^)]*)\)\s*;",
    re.MULTILINE,
)

REPO = Path(__file__).resolve().parent.parent
DESTINATION = REPO / "user" / "libbboeos" / "libbboeos_stubs.S"
INCLUDE_DIRECTORY = REPO / "user" / "libbboeos" / "include"
SOURCE = REPO / "kernel" / "include" / "constants.asm"


def _collect_function_constants() -> dict[str, int]:
    """Return {NAME: VALUE} for every `%assign FUNCTION_<...>` in constants.asm.

    Values resolve to absolute integers.  Handles forward references by
    looping until a pass adds nothing new.  NASM hex literals (`0x...` or
    trailing `h`) and decimal integers are accepted.
    """
    raw: dict[str, str] = {}
    for line in SOURCE.read_text().splitlines():
        match = ASSIGN.match(line)
        if match is None:
            continue
        raw[match.group("name")] = match.group("value").strip()
    resolved: dict[str, int] = {}
    while True:
        progress = False
        for name, value in raw.items():
            if name in resolved:
                continue
            integer = _try_evaluate(value=value, environment=resolved)
            if integer is None:
                continue
            resolved[name] = integer
            progress = True
        if not progress:
            break
    return {name: value for name, value in resolved.items() if name.startswith("FUNCTION_")}


def _collect_prototype_parameter_counts() -> dict[str, int | None]:
    """Return {function_name: parameter_count} from libbboeos headers.

    Variadic prototypes (``...``) map to None — they stay cdecl.
    ``(void)`` maps to 0.
    """
    result: dict[str, int | None] = {}
    for header in sorted(INCLUDE_DIRECTORY.glob("*.h")):
        for match in PROTOTYPE.finditer(header.read_text()):
            name = match.group(1)
            parameters = match.group(2).strip()
            if "..." in parameters:
                result[name] = None
            elif parameters in {"", "void"}:
                result[name] = 0
            else:
                result[name] = parameters.count(",") + 1
    return result


def _render_stubs(*, exports: list[tuple[str, int, int | None]]) -> str:
    """Render libbboeos_stubs.S for the given (name, address, parameter_count) tuples."""
    lines = [
        "/* user/libbboeos/libbboeos_stubs.S — auto-generated.  DO NOT EDIT.",
        " *",
        " * Regenerate with `python3 tools/generate_libbboeos_stubs.py`.",
        " * Each stub shuffles cdecl stack arguments into regparm",
        " * registers (EAX/EDX/ECX) then jumps to the shared",
        " * libbboeos blob via `jmp [FUNCTION_<NAME>_PTR]`.",
        " * Clang programs link this file BEFORE libbboeos.a so ld",
        " * resolves each export to the stub and never pulls the",
        " * full body out of the archive.",
        " *",
        " * Source of truth: FUNCTION_<NAME>_PTR offsets in",
        " * kernel/include/constants.asm + prototypes in",
        " * user/libbboeos/include/*.h.  Sorted alphabetically.",
        " */",
        "",
        "        .intel_syntax noprefix",
        '        .section .text.libbboeos_stubs, "ax", @progbits',
        "",
    ]
    regparm_registers = ["eax", "edx", "ecx"]
    for name, address, parameter_count in exports:
        symbol = name.lower()
        lines.extend([
            f"        .globl {symbol}",
            f"        .type  {symbol}, @function",
            f"{symbol}:",
        ])
        if parameter_count is not None and parameter_count > 0:
            regparm_count = min(3, parameter_count)
            for i in range(regparm_count):
                offset = 4 + i * 4
                lines.append(f"        mov {regparm_registers[i]}, [esp+{offset}]")
        lines.extend([
            f"        jmp [0x{address:08x}]    /* FUNCTION_{name}_PTR */",
            f"        .size {symbol}, . - {symbol}",
            "",
        ])
    return "\n".join(lines)


def _try_evaluate(*, environment: dict[str, int], value: str) -> int | None:
    """Try to evaluate a NASM `%assign` RHS using already-resolved names.

    Returns None if any token references an unresolved name.
    """
    normalized = re.sub(r"\b([0-9A-Fa-f]+)h\b", r"0x\1", value)
    tokens = re.findall(r"\w+|[+\-*/()]", normalized)
    expression_parts: list[str] = []
    for token in tokens:
        if re.fullmatch(r"\w+", token) and not re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", token):
            if token not in environment:
                return None
            expression_parts.append(str(environment[token]))
        else:
            expression_parts.append(token)
    try:
        return int(eval(" ".join(expression_parts), {"__builtins__": {}}, {}))
    except (NameError, SyntaxError, TypeError, ValueError, ZeroDivisionError):
        return None


def main() -> int:
    """Regenerate libbboeos_stubs.S from constants.asm; idempotent."""
    constants = _collect_function_constants()
    exports: list[tuple[str, int]] = []
    for full_name, address in constants.items():
        if not full_name.endswith("_PTR"):
            continue
        base = full_name[len("FUNCTION_") : -len("_PTR")]
        legacy = f"FUNCTION_{base}"
        if legacy in constants:
            continue
        exports.append((base, address))
    exports.sort()

    prototypes = _collect_prototype_parameter_counts()
    exports_with_parameters: list[tuple[str, int, int | None]] = []
    for name, address in exports:
        parameter_count = prototypes.get(name.lower())
        exports_with_parameters.append((name, address, parameter_count))

    new = _render_stubs(exports=exports_with_parameters)
    if DESTINATION.exists() and DESTINATION.read_text() == new:
        return 0
    DESTINATION.write_text(new)
    print(f"wrote {DESTINATION.relative_to(REPO)} ({len(exports)} stubs)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
