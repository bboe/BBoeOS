#!/usr/bin/env python3
"""Emit vdso_pointers.bin from a NASM map of user/vdso/vdso.asm.

The vDSO's FUNCTION_POINTER_TABLE (at user-virt 0x10800) is the
linker-friendly indirect-call vector parallel to FUNCTION_TABLE.  Its
values are the absolute virtual addresses of the shared_* helper
bodies, which only NASM knows after assembling vdso.asm.  This tool
parses the map file, looks up each helper symbol, and writes a flat
52-byte binary containing 13 little-endian 4-byte addresses in the
order required by the FUNCTION_*_PTR constants in
kernel/include/constants.asm.  kernel.asm incbins the output blob and
vdso_install copies it into the live vDSO page at boot.
"""

import argparse
import re
import struct
import sys
from pathlib import Path

# Order must match the FUNCTION_*_PTR constants in
# kernel/include/constants.asm and the function_table jmp order in
# user/vdso/vdso.asm.
HELPER_ORDER = (
    "shared_die",
    "shared_exit",
    "shared_get_character",
    "shared_print_byte_decimal",
    "shared_print_character",
    "shared_print_datetime",
    "shared_print_decimal",
    "shared_print_hex",
    "shared_print_ip",
    "shared_print_mac",
    "shared_print_string",
    "shared_printf",
    "shared_write_stdout",
)


def _parse_map(*, map_path: Path) -> dict[str, int]:
    """Return {symbol: absolute_value} for every row of the NASM map.

    Two row shapes are recognised:
      - "<value>  <name>"               (No-Section / equ / %assign)
      - "<real>  <virtual>  <name>"     (section labels)
    For section labels the virtual column (column 2) is the relevant
    runtime address — vdso.asm sets `org 0x10000`, so column 2 reflects
    that.  No-Section rows have a single value column.
    """
    addresses: dict[str, int] = {}
    pattern_section = re.compile(r"^\s+([0-9A-Fa-f]+)\s+([0-9A-Fa-f]+)\s+(\S+)\s*$")
    pattern_no_section = re.compile(r"^\s+([0-9A-Fa-f]+)\s+(\S+)\s*$")
    for raw_line in map_path.read_text(encoding="utf-8").splitlines():
        match = pattern_section.match(raw_line)
        if match:
            addresses[match.group(3)] = int(match.group(2), 16)
            continue
        match = pattern_no_section.match(raw_line)
        if match:
            addresses[match.group(2)] = int(match.group(1), 16)
    return addresses


def main() -> int:
    """CLI entry point: parse vdso.map, write vdso_pointers.bin."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map_path", type=Path)
    parser.add_argument("output_path", type=Path)
    arguments = parser.parse_args()
    addresses = _parse_map(map_path=arguments.map_path)
    missing = [name for name in HELPER_ORDER if name not in addresses]
    if missing:
        print(f"gen_vdso_pointers.py: missing symbols in {arguments.map_path}: {missing}", file=sys.stderr)
        return 1
    payload = b"".join(struct.pack("<I", addresses[name]) for name in HELPER_ORDER)
    arguments.output_path.write_bytes(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
