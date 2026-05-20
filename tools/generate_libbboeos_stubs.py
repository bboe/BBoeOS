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

REPO = Path(__file__).resolve().parent.parent
DESTINATION = REPO / "user" / "libbboeos" / "libbboeos_stubs.S"
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


def _render_stubs(*, exports: list[tuple[str, int]]) -> str:
    """Render libbboeos_stubs.S for the given (export_name, pointer_address) pairs."""
    lines = [
        "/* user/libbboeos/libbboeos_stubs.S — auto-generated.  DO NOT EDIT.",
        " *",
        " * Regenerate with `python3 tools/generate_libbboeos_stubs.py`.",
        " * Each stub is a 6-byte `jmp [FUNCTION_<NAME>_PTR]` thunk into the",
        " * shared libbboeos blob.  Clang programs link this file BEFORE",
        " * libbboeos.a so ld resolves each export to the stub and never",
        " * pulls the full body out of the archive — that's the Phase 4",
        " * binary-size win (per-program string.c bodies retire to the",
        " * shared blob).",
        " *",
        " * Source of truth: FUNCTION_<NAME>_PTR offsets in",
        " * kernel/include/constants.asm.  Sorted alphabetically to match.",
        " */",
        "",
        "        .intel_syntax noprefix",
        '        .section .text.libbboeos_stubs, "ax", @progbits',
        "",
    ]
    for name, address in exports:
        symbol = name.lower()
        lines.extend([
            f"        .globl {symbol}",
            f"        .type  {symbol}, @function",
            f"{symbol}:",
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

    new = _render_stubs(exports=exports)
    if DESTINATION.exists() and DESTINATION.read_text() == new:
        return 0
    DESTINATION.write_text(new)
    print(f"wrote {DESTINATION.relative_to(REPO)} ({len(exports)} stubs)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
