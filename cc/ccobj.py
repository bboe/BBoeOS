"""cc.py pack-ccobj: package a NASM .bin + .lst into a .ccobj JSON.

The .ccobj format is documented in
docs/superpowers/specs/2026-05-16-cc-object-files-design.md § "Object
file format".  Schema is version 1; new reloc types or section kinds
bump the version.
"""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Maps CCREL_* macro names to (reloc_type, opcode_length).  Opcode
# length is the number of bytes between the macro's first emitted byte
# and the 4-byte placeholder.  For every macro in
# src/include/ccobj_markers.inc the opcode is a single byte
# (E8 / E9 / A1 / A3), so opcode_length == 1.
CCREL_MACROS: dict[str, tuple[str, int]] = {
    "CCREL_CALL": ("rel32", 1),
    "CCREL_JMP": ("rel32", 1),
    "CCREL_MOVABS_LOAD_EAX": ("abs32", 1),
    "CCREL_MOVABS_STORE_EAX": ("abs32", 1),
}

# Default alignment per section.  Linker uses
# max(per-object align, per-section default).
DEFAULT_ALIGN: dict[str, int] = {"text": 16, "rodata": 4, "data": 4, "bss": 4}

# Sections the linker knows about.  Other sections in the .lst are an
# error (keeps the format tight).
KNOWN_SECTIONS: tuple[str, ...] = ("text", "rodata", "data", "bss")

RE_BSS_RES = re.compile(r"^res([bwd])\s+(\d+)\s*$")
RE_GLOBAL = re.compile(r"^global\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")
RE_LABEL = re.compile(r"^([A-Za-z_.][A-Za-z0-9_.]*):")
RE_MACRO = re.compile(r"^(CCREL_[A-Z_]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")
RE_SECTION = re.compile(r"^section\s+\.([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _accumulate_bytes(*, bytes_column: bytes, offset: int, section_buffer: bytearray) -> None:
    r"""Write ``bytes_column`` into ``section_buffer`` at ``offset``.

    Extends the buffer with ``\x00`` if it doesn't reach that position yet.
    """
    end = offset + len(bytes_column)
    if len(section_buffer) < end:
        section_buffer.extend(b"\x00" * (end - len(section_buffer)))
    section_buffer[offset:end] = bytes_column


def _parse_listing_row(*, raw_line: str) -> tuple[int | None, bytes, str] | None:
    """Parse one NASM .lst row into ``(offset, bytes_column, source)``.

    Returns None for blank lines.  ``offset`` is None for rows with no
    emit column.  ``bytes_column`` is b"" for non-emitting rows or
    rows whose bytes column is a ``<res ...>`` BSS-reservation marker.
    """
    stripped = raw_line.strip()
    if not stripped or not stripped[0].isdigit():
        return None
    # After the line-number column, the rest is either
    #   "<8-hex-offset> <bytes> [<N>] <source>"   (emitting)
    # or
    #   "[<N>] <source>"                          (non-emitting)
    parts = stripped.split(" ", 1)
    if len(parts) < 2:
        return None
    rest = parts[1].lstrip()
    offset: int | None = None
    bytes_column = b""
    source = rest
    if len(rest) >= 8 and all(c in "0123456789ABCDEFabcdef" for c in rest[:8]) and (len(rest) == 8 or rest[8] in " \t"):
        offset = int(rest[:8], 16)
        after_offset = rest[8:].lstrip()
        # Bytes column: either a run of hex pairs (optionally with
        # single spaces between dwords) ended by 2+ spaces / tab /
        # `<N>`, or the special `<res Nh>` marker.
        if after_offset.startswith("<res "):
            end_of_marker = after_offset.index(">") + 1
            bytes_column = b""
            after_bytes = after_offset[end_of_marker:].lstrip()
        else:
            bytes_match = re.match(
                r"^([0-9A-Fa-f]+(?:\s[0-9A-Fa-f]+)*)(\s{2,}|\t|\s+<|\s*$)",
                after_offset,
            )
            if bytes_match:
                hex_string = bytes_match.group(1).replace(" ", "")
                if len(hex_string) % 2 == 0:
                    try:
                        bytes_column = bytes.fromhex(hex_string)
                    except ValueError:
                        bytes_column = b""
                after_bytes = after_offset[bytes_match.end(1) :].lstrip()
            else:
                after_bytes = after_offset
        source = re.sub(r"^<\d+>\s+", "", after_bytes)
    else:
        source = re.sub(r"^<\d+>\s+", "", rest)
    return offset, bytes_column, source


def pack_ccobj(*, bin_path: Path, lst_path: Path, output_path: Path) -> None:
    """Read a NASM .bin + .lst pair, write a .ccobj JSON."""
    listing_lines = lst_path.read_text(encoding="utf-8").splitlines()
    # Read the .bin for cross-validation only; the per-section bytes
    # written into the .ccobj are reconstructed from the listing.
    _bin_bytes = bin_path.read_bytes()

    section_bytes: dict[str, bytearray] = {}
    bss_size: int = 0
    symbols: dict[str, dict] = {}
    globals_declared: set[str] = set()
    relocations: list[dict] = []
    extern_set: list[str] = []  # ordered for deterministic output

    current_section: str | None = None
    pending_macro: tuple[str, str] | None = None  # (macro_name, symbol)

    for raw_line in listing_lines:
        parsed = _parse_listing_row(raw_line=raw_line)
        if parsed is None:
            continue
        offset, bytes_column, source = parsed
        source_stripped = source.lstrip()

        section_match = RE_SECTION.match(source_stripped)
        if section_match:
            name = section_match.group(1)
            if name not in KNOWN_SECTIONS:
                message = f"unknown section .{name} in {lst_path}"
                raise ValueError(message)
            current_section = name
            section_bytes.setdefault(name, bytearray())
            continue

        global_match = RE_GLOBAL.match(source_stripped)
        if global_match:
            globals_declared.add(global_match.group(1))
            continue

        # Strip an optional "label:" prefix from the source so BSS and
        # macro patterns match the remainder rather than the full line.
        # Record any label found before dispatching to sub-patterns.
        label_name: str | None = None
        remainder = source_stripped
        label_match = RE_LABEL.match(source_stripped)
        if label_match and current_section is not None:
            label_name = label_match.group(1)
            remainder = source_stripped[label_match.end() :].lstrip()

        bss_match = RE_BSS_RES.match(remainder)
        if bss_match and current_section == "bss":
            unit = {"b": 1, "w": 2, "d": 4}[bss_match.group(1)]
            count = int(bss_match.group(2))
            bss_size += unit * count

        macro_match = RE_MACRO.match(source_stripped)
        if macro_match:
            macro_name, symbol = macro_match.group(1), macro_match.group(2)
            if macro_name not in CCREL_MACROS:
                message = f"unknown CCREL_* macro {macro_name!r} in {lst_path}"
                raise ValueError(message)
            pending_macro = (macro_name, symbol)
            continue

        if label_name is not None:
            symbol_offset = offset if offset is not None else len(section_bytes.setdefault(current_section, bytearray()))
            symbols[label_name] = {
                "section": current_section,
                "offset": symbol_offset,
                "binding": "global" if label_name in globals_declared else "local",
            }

        if offset is not None and bytes_column and current_section is not None:
            if pending_macro is not None:
                macro_name, symbol = pending_macro
                reloc_type, opcode_length = CCREL_MACROS[macro_name]
                relocations.append({
                    "section": current_section,
                    "offset": offset + opcode_length,
                    "symbol": symbol,
                    "type": reloc_type,
                })
                if symbol not in extern_set and symbol not in globals_declared:
                    extern_set.append(symbol)
                pending_macro = None
            _accumulate_bytes(
                bytes_column=bytes_column,
                offset=offset,
                section_buffer=section_bytes.setdefault(current_section, bytearray()),
            )

    sections: dict[str, dict] = {}
    for name in KNOWN_SECTIONS:
        if name == "bss":
            if bss_size > 0:
                sections["bss"] = {"size": bss_size, "align": DEFAULT_ALIGN["bss"]}
            continue
        if name in section_bytes and len(section_bytes[name]) > 0:
            sections[name] = {
                "bytes": base64.b64encode(bytes(section_bytes[name])).decode("ascii"),
                "align": DEFAULT_ALIGN[name],
            }

    output = {
        "version": 1,
        "source": str(lst_path.with_suffix(".asm")),
        "sections": sections,
        "symbols": symbols,
        "extern": extern_set,
        "relocations": relocations,
    }
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(output, output_file, indent=2)
        output_file.write("\n")
