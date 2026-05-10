#!/usr/bin/env python3
"""Generate tools/libc/include/syscalls.h from src/include/constants.asm.

Single source of truth for kernel-userspace ABI numbers: the kernel reads
constants.asm directly (NASM %assign), and clang-compiled userland
(tools/libc, tools/doom) reads the generated C header.  Eliminates the
class of bugs where renumbering the asm side leaves hardcoded hex
literals on the C side pointing at the wrong syscall — see the
SYS_RTC_MILLIS regression that wedged Doom's main loop after PR #337
shifted 0x31 -> 0x32.

Filtered to the prefixes that matter to userland: SYS_, ERROR_,
SIG[A-Z], FD_TYPE_, *_IOCTL_, VDSO_.  Other constants in the asm file
(memory layout, paging, port numbers) stay asm-only because nothing in
the C-compiled userland references them.

Re-run is idempotent — the script writes the file only if the contents
differ, so make / build_doom.py can call it unconditionally without
forcing recompiles.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# `%assign NAME VALUE [; comment]` with optional leading whitespace.
ASSIGN = re.compile(r"^\s*%assign\s+(?P<name>\w+)\s+(?P<value>\S+)\s*(?:;(?P<comment>.*))?$")

# GROUPS order is load-bearing in two ways:
# 1. Section order in the generated header — entries print top-to-bottom in
#    GROUPS order, so put the most-referenced groups (SYS_, ERROR_) ahead of
#    niche ones (VDSO_) so a reader scanning the file lands on syscall
#    numbers without scrolling past everything else.
# 2. Classification is first-match-wins (the loop in `_render_header` breaks
#    on the first matching pattern), so if a name ever satisfied two
#    patterns it'd land in the earlier group's section.  Today's patterns
#    are disjoint by prefix so (2) is dormant, but the rule lets a future
#    overlap-friendly pattern (say, a generic `^.*_IOCTL_` catch-all) be
#    added without re-thinking the contract — just put the more-specific
#    pattern earlier.
GROUPS = [
    ("Syscall numbers (AH on INT 30h)", re.compile(r"^SYS_")),
    ("Error codes (AL when CF set)", re.compile(r"^ERROR_")),
    ("Signal numbers", re.compile(r"^SIG[A-Z]")),
    ("File-descriptor types", re.compile(r"^FD_TYPE_")),
    ("ioctl commands", re.compile(r"^(MIDI|CONSOLE|AUDIO|VGA)_IOCTL_")),
    ("vDSO offsets", re.compile(r"^VDSO_")),
]

# REPO has to come before DESTINATION and SOURCE because they reference it;
# DESTINATION before SOURCE keeps the path block alphabetical thereafter.
REPO = Path(__file__).resolve().parent.parent
DESTINATION = REPO / "tools" / "libc" / "include" / "syscalls.h"
SOURCE = REPO / "src" / "include" / "constants.asm"


def _format_value(*, raw: str) -> str:
    """Convert a NASM literal to a C literal.

    NASM accepts ``01h`` / ``0F0h`` (hex with trailing h, leading zero
    if the first digit is a letter) and ``0x...`` and plain decimal.
    Emit C-style ``0x..`` for hex, decimal otherwise — matches the
    style the kernel asm uses and stays readable when grepping for a
    syscall number.
    """
    if raw.endswith(("h", "H")):
        digits = raw[:-1].lstrip("0") or "0"
        return f"0x{digits.upper()}"
    if raw.startswith(("0x", "0X")):
        return f"0x{raw[2:].upper()}"
    return raw


def _parse_assignments(*, source: Path) -> list[tuple[str, str, str]]:
    """Parse `%assign NAME VALUE [; comment]` lines.

    Returns a list of ``(name, c_value, comment)`` tuples in source order.
    Skips lines whose value isn't a plain integer literal — references
    to other constants (``KERNEL_BASE + 0x100``) belong on the asm side.
    """
    rows: list[tuple[str, str, str]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        match = ASSIGN.match(line)
        if match is None:
            continue
        raw = match["value"]
        # Plain integer literal only: digits + optional h suffix, or 0x prefix.
        if not re.fullmatch(r"(0x[0-9A-Fa-f]+|[0-9A-Fa-f]+h|\d+)", raw):
            continue
        rows.append((match["name"], _format_value(raw=raw), (match["comment"] or "").strip()))
    return rows


def _render_header(*, rows: list[tuple[str, str, str]]) -> str:
    """Group rows by GROUPS and emit one section per group.

    Within a section, rows stay in constants.asm source order so a diff
    against the asm shows trivially what was added/moved.  Names that
    don't match any group prefix are dropped — they're either asm-only
    (memory layout / port numbers) or new prefixes that need a GROUPS
    entry added consciously.
    """
    by_group: dict[int, list[tuple[str, str, str]]] = {index: [] for index in range(len(GROUPS))}
    for name, value, comment in rows:
        for index, (_, pattern) in enumerate(GROUPS):
            if pattern.match(name):
                by_group[index].append((name, value, comment))
                break
    width = max((len(name) for name, _, _ in rows if any(p.match(name) for _, p in GROUPS)), default=0)
    lines: list[str] = [
        "/* AUTO-GENERATED from src/include/constants.asm by tools/generate_syscalls_h.py.",
        " * DO NOT EDIT MANUALLY — re-run the generator to refresh.",
        " *",
        " * Mirrors the syscall-ABI %assign constants (numbers + error codes +",
        " * signals + fd types + ioctl commands + vDSO offsets) into a C header",
        " * so clang-compiled userland (tools/libc, tools/doom) can reference",
        " * them by name instead of hardcoding numeric values that drift the",
        " * next time the asm side is renumbered. */",
        "",
        "#ifndef BBOEOS_LIBC_SYSCALLS_H",
        "#define BBOEOS_LIBC_SYSCALLS_H",
        "",
        "/* Stringify a macro's expansion so it can be embedded inside an inline-",
        ' * asm string literal: `"mov $" SYSNUM_STR(SYS_RTC_MILLIS) ", %%ah\\n"`',
        ' * preprocesses to `"mov $" "0x32" ", %%ah\\n"` which the C compiler',
        " * adjacent-concatenates into one literal.  The two-level expansion is",
        " * required so the macro's value is stringified, not its name. */",
        "#define _SYSNUM_STR2(x) #x",
        "#define SYSNUM_STR(x) _SYSNUM_STR2(x)",
        "",
    ]
    for index, (label, _) in enumerate(GROUPS):
        entries = by_group[index]
        if not entries:
            continue
        lines.append(f"/* {label} */")
        for name, value, comment in entries:
            line = f"#define {name.ljust(width)} {value}"
            if comment:
                line = f"{line}  /* {comment} */"
            lines.append(line)
        lines.append("")
    lines.append("#endif /* BBOEOS_LIBC_SYSCALLS_H */")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Read constants.asm, render header, write only on change."""
    if not SOURCE.is_file():
        print(f"generate_syscalls_h: missing {SOURCE}", file=sys.stderr)
        return 1
    rows = _parse_assignments(source=SOURCE)
    rendered = _render_header(rows=rows)
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    if DESTINATION.is_file() and DESTINATION.read_text(encoding="utf-8") == rendered:
        return 0
    DESTINATION.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
