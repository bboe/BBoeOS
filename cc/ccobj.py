"""cc.py pack-ccobj: package a NASM .bin + .lst into a .ccobj JSON."""

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

# NASM listing bytes-column tokens.  Each line's bytes column is a
# sequence of these tokens:
#   ``XXXX`` — plain hex pairs (real instruction bytes).
#   ``XX YY ZZ`` — separated hex (NASM puts a space between dwords).
#   ``????????`` — ``?`` digits for ``resb`` / ``resw`` / ``resd``
#                  reservations inside ``section .bss``.
#   ``[YYYYYYYY]`` — unresolved cross-section abs32 placeholder
#                    (linker patches the 4-byte slot at this offset).
#   ``(YYYYYYYY)`` — already-resolved absolute-numeric reference
#                    (bytes are correct; no relocation needed).
# The column ends with an optional ``-`` line-continuation marker
# (long ``dd`` rows spill to a follow-up line with no source column),
# then a whitespace gap before the source column.
RE_BYTES_TOKEN = re.compile(
    r"([0-9A-Fa-f?]+)"  # plain hex run (group 1)
    r"|\[([0-9A-Fa-f]+)\]"  # bracketed placeholder (group 2)
    r"|\(([0-9A-Fa-f]+)\)"  # parenthesised absolute (group 3)
)
RE_BYTES_TERMINATOR = re.compile(r"^(-?)(\s{2,}|\t|\s+<|\s*$)")
# Generic identifier scanner over the source column of a NASM listing
# row.  When the bytes column carried a ``[YYYYYYYY]`` cross-section
# placeholder, ``pack_ccobj`` intersects the identifiers found on that
# row's source with the pre-scanned set of every label defined in this
# listing; the unique survivor is the relocation target.  Using the
# label set as the filter (rather than a prefix regex over the source)
# means asm-symbol-globals like ``arp_frame`` resolve as cleanly as
# cc.py-prefixed symbols like ``_g_x`` / ``_str_0`` / ``_ir_s0``.
RE_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_.])([.A-Za-z_][A-Za-z0-9_.]*)")
RE_GLOBAL = re.compile(r"^global\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")
RE_LABEL = re.compile(r"^([A-Za-z_.][A-Za-z0-9_.]*):")
# NASM accepts a colon-less label form when the row begins with an
# identifier directly followed by a data / reservation / EQU
# directive — e.g. ``STR_ENDMACRO db 'endmacro',0``.  cc.py emits
# this form from file-scope inline asm blocks in ``src/c/asm.c`` (the
# self-hosted assembler), so the relocation pre-scan needs to pick
# these up as defined labels too.  Restricted to a fixed directive
# whitelist so plain instruction rows (``mov eax, STR_FOO``) never
# get misread as label definitions.
RE_LABEL_NO_COLON = re.compile(r"^([A-Za-z_.][A-Za-z0-9_.]*)(?=\s+(?:db|dw|dd|dq|do|resb|resw|resd|resq|reso|times|equ)\b)")
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


def _parse_listing_row(*, raw_line: str) -> tuple[int | None, bytes, int | None, int, str] | None:
    """Parse one NASM .lst row into ``(offset, bytes_column, reloc_opcode_length, bss_increment, source)``.

    Returns None for blank lines.  ``offset`` is None for rows with no
    emit column.  ``bytes_column`` is b"" for non-emitting rows or
    rows whose bytes column is a ``<res ...>`` BSS-reservation marker.

    ``reloc_opcode_length`` is non-None when the bytes column is a
    ``XX[YYYYYYYY]`` form — NASM emits brackets around a 4-byte
    placeholder when the operand is an unresolved cross-section
    reference (e.g. ``push _str_0`` from .text → .rodata, or
    ``mov eax, [_g_x]`` from .text → .data).  The opcode-length
    value is the number of bytes BEFORE the placeholder (i.e. the
    relocation patch site = ``offset + reloc_opcode_length``).  The
    caller emits an abs32 relocation at that site, with the symbol
    name extracted from the source column.

    NASM also uses ``XX(YYYYYYYY)`` parentheses for operands that are
    absolute-numeric expressions (e.g. ``jmp FUNCTION_EXIT`` where
    ``FUNCTION_EXIT`` is a ``%assign``-constant).  Those bytes are
    already correct as emitted — no relocation needed — so the
    parser captures them like plain hex but leaves
    ``reloc_opcode_length`` None.

    ``bss_increment`` is the byte count when the bytes column was a
    ``<res Nh>`` marker (NASM's representation for ``resb`` /
    ``resw`` / ``resd`` reservations); zero otherwise.  Using the
    marker rather than re-parsing the source's ``resb N`` directive
    lets pack_ccobj handle symbolic sizes like ``resb MAX_PATH``
    (NASM has already resolved ``MAX_PATH`` to a literal byte count
    by the time it emits the marker).
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
    reloc_opcode_length: int | None = None
    bss_increment = 0
    source = rest
    if len(rest) >= 8 and all(c in "0123456789ABCDEFabcdef" for c in rest[:8]) and (len(rest) == 8 or rest[8] in " \t"):
        offset = int(rest[:8], 16)
        after_offset = rest[8:].lstrip()
        # Bytes column: either a run of hex pairs (optionally with
        # single spaces between dwords) ended by 2+ spaces / tab /
        # `<N>`, an ``XX[YYYYYYYY]`` / ``XX(YYYYYYYY)`` form for
        # cross-section / absolute-numeric operands, or the special
        # ``<res Nh>`` BSS-reservation marker.
        if after_offset.startswith("<res "):
            end_of_marker = after_offset.index(">") + 1
            # NASM format: ``<res Nh>`` where N is hex bytes.
            marker_body = after_offset[len("<res ") : end_of_marker - 1].strip()
            if marker_body.endswith("h"):
                try:
                    bss_increment = int(marker_body[:-1], 16)
                except ValueError:
                    bss_increment = 0
            bytes_column = b""
            after_bytes = after_offset[end_of_marker:].lstrip()
        else:
            # Token-scan the bytes column.  Each token contributes a
            # run of bytes to ``bytes_column``; a bracketed token also
            # sets ``reloc_opcode_length`` to the count of bytes that
            # precede the placeholder on this row, so the caller can
            # emit an abs32 relocation at ``offset + reloc_opcode_length``.
            full_hex = ""
            cursor = 0
            saw_token = False
            while True:
                token_match = RE_BYTES_TOKEN.match(after_offset, cursor)
                if token_match is None:
                    break
                if (group_hex := token_match.group(1)) is not None:
                    full_hex += group_hex
                elif (group_bracket := token_match.group(2)) is not None:
                    if reloc_opcode_length is None:
                        reloc_opcode_length = len(full_hex) // 2
                    full_hex += group_bracket
                else:
                    full_hex += token_match.group(3) or ""
                cursor = token_match.end()
                saw_token = True
                next_char = after_offset[cursor : cursor + 1]
                if next_char and next_char not in "0123456789ABCDEFabcdef[(":
                    break
            if saw_token:
                if "?" in full_hex:
                    # ``resb`` reservation — NASM renders each byte as
                    # ``??`` (e.g. ``????????`` for ``resb 4``).
                    # Larger / symbolic reservations also surface the
                    # ``<res Nh>`` marker above; either way the byte
                    # count is recorded into ``bss_increment``, and
                    # ``bytes_column`` stays empty (no on-disk bytes).
                    bytes_column = b""
                    bss_increment = len(full_hex) // 2
                elif len(full_hex) % 2 == 0:
                    try:
                        bytes_column = bytes.fromhex(full_hex)
                    except ValueError:
                        bytes_column = b""
                after_bytes = after_offset[cursor:]
                terminator_match = RE_BYTES_TERMINATOR.match(after_bytes)
                if terminator_match:
                    after_bytes = after_bytes[terminator_match.end() :]
                after_bytes = after_bytes.lstrip()
            else:
                after_bytes = after_offset
        source = re.sub(r"^<\d+>\s+", "", after_bytes)
    else:
        source = re.sub(r"^<\d+>\s+", "", rest)
    return offset, bytes_column, reloc_opcode_length, bss_increment, source


def _prescan_defined_labels(*, listing_lines: list[str]) -> set[str]:
    """Pre-scan pass: collect every label defined in this listing.

    Used to disambiguate the relocation target when a NASM listing row
    has bracketed placeholder bytes (``XX[YYYYYYYY]``): the relocation
    target is the unique identifier on that row's source line that
    also appears as a defined label somewhere in the listing.  Two
    passes are necessary because forward references (e.g. a code site
    in .text referring to a label defined later in .data or .bss) can't
    be resolved during a single forward sweep of the listing.
    """
    defined: set[str] = set()
    for raw_line in listing_lines:
        parsed = _parse_listing_row(raw_line=raw_line)
        if parsed is None:
            continue
        _offset, _bytes_column, _reloc_opcode_length, _bss_increment, source = parsed
        source_stripped = source.lstrip()
        label_match = RE_LABEL.match(source_stripped) or RE_LABEL_NO_COLON.match(source_stripped)
        if label_match:
            defined.add(label_match.group(1))
    return defined


def pack_ccobj(*, bin_path: Path, lst_path: Path, output_path: Path) -> None:
    """Read a NASM .bin + .lst pair, write a .ccobj JSON."""
    listing_lines = lst_path.read_text(encoding="utf-8").splitlines()
    # Read the .bin for cross-validation only; the per-section bytes
    # written into the .ccobj are reconstructed from the listing.
    _bin_bytes = bin_path.read_bytes()

    defined_labels = _prescan_defined_labels(listing_lines=listing_lines)

    section_bytes: dict[str, bytearray] = {}
    bss_size: int = 0
    symbols: dict[str, dict] = {}
    globals_declared: set[str] = set()
    relocations: list[dict] = []
    extern_set: list[str] = []  # ordered for deterministic output

    current_section: str | None = None
    pending_macro: tuple[str, str] | None = None  # (macro_name, symbol)
    # Multi-symbol data directives (e.g. ``_arr_0: dd _str_4, _str_5,
    # _str_6``) emit one bracketed placeholder per item across NASM
    # listing continuation rows.  Only the parent row carries the
    # source text; continuation rows are bytes-only.  When the parent
    # row is processed below we seed this queue with the defined-label
    # identifiers from its source, in order, and each bracket-form
    # relocation pops the next one.
    pending_reloc_symbols: list[str] = []

    for raw_line in listing_lines:
        parsed = _parse_listing_row(raw_line=raw_line)
        if parsed is None:
            continue
        offset, bytes_column, reloc_opcode_length, bss_increment, source = parsed
        source_stripped = source.lstrip()

        if source_stripped:
            # Source-bearing row — refresh the relocation queue from
            # its identifiers (excluding any label being defined here,
            # which is the data sink, not a relocation target).
            label_lookahead = RE_LABEL.match(source_stripped) or RE_LABEL_NO_COLON.match(source_stripped)
            line_label = label_lookahead.group(1) if label_lookahead else None
            pending_reloc_symbols = [
                match.group(1)
                for match in RE_IDENTIFIER.finditer(source_stripped)
                if match.group(1) in defined_labels and match.group(1) != line_label
            ]

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
        # Both colon-form (``foo: db ...``) and colon-less form
        # (``foo db ...`` — NASM's compact data-declaration shorthand,
        # used by file-scope inline asm in src/c/asm.c) are accepted.
        label_name: str | None = None
        label_match = RE_LABEL.match(source_stripped) or RE_LABEL_NO_COLON.match(source_stripped)
        if label_match and current_section is not None:
            label_name = label_match.group(1)

        if bss_increment > 0 and current_section == "bss":
            # NASM emits ``<res Nh>`` in the bytes column whenever a
            # row reserves BSS bytes, and N is the resolved literal
            # byte count regardless of whether the source spelled the
            # size as a digit (``resb 4``) or as a symbolic constant
            # (``resb MAX_PATH``).  Trusting the marker is more
            # robust than re-parsing the source's ``resb`` directive.
            bss_size += bss_increment

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
            elif reloc_opcode_length is not None:
                # NASM listing carried a ``XX[YYYYYYYY]`` placeholder —
                # an unresolved cross-section absolute reference.  The
                # bytes are already in ``bytes_column``; emit an abs32
                # relocation so the linker patches the 4-byte
                # placeholder with the symbol's final address.  The
                # target is the next pending relocation symbol seeded
                # by the most recent source-bearing row.  This handles
                # both single-symbol references (``mov eax, [_g_x]``)
                # and multi-symbol data directives (``dd a, b, c``)
                # whose placeholders span continuation rows with no
                # source column.
                if not pending_reloc_symbols:
                    message = (
                        f"unresolved cross-section reference in {lst_path} at offset {offset:#x} "
                        f"but no defined-label identifier available (source was {source!r})"
                    )
                    raise ValueError(message)
                symbol = pending_reloc_symbols.pop(0)
                relocations.append({
                    "section": current_section,
                    "offset": offset + reloc_opcode_length,
                    "symbol": symbol,
                    "type": "abs32",
                })
            _accumulate_bytes(
                bytes_column=bytes_column,
                offset=offset,
                section_buffer=section_bytes.setdefault(current_section, bytearray()),
            )

    # An extern reference whose symbol is also defined locally in this
    # same .ccobj (e.g. a forward-declared function — ``CCREL_CALL`` is
    # emitted at the call site before the definition is parsed) is
    # resolvable in-object; the linker should not treat it as an
    # unresolved cross-object extern.  Drop those names from
    # ``extern_set`` so ccld only sees true externs.
    extern_set = [name for name in extern_set if name not in symbols]

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
