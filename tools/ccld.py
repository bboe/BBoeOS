#!/usr/bin/env python3
"""cc.py object-file linker.

Reads one or more `.ccobj` files (and optionally `.ccar` archives) and
emits a flat binary loadable by program_enter at PROGRAM_BASE.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

BSS_MAGIC32 = 0xB032
BSS_TRAILER_SIZE = 6  # struct.calcsize("<IH"): dd bss_size + dw BSS_MAGIC32
CCAR_VERSION = 1
CCOBJ_VERSION = 1
DEFAULT_ALIGN: dict[str, int] = {"data": 4, "rodata": 4, "text": 16}
KNOWN_BINDINGS: frozenset[str] = frozenset({"global", "local"})
KNOWN_RELOCATION_TYPES: tuple[str, ...] = ("abs32", "rel32")
KNOWN_SECTIONS = ("bss", "data", "rodata", "text")
MAX_BSS_SIZE = 0xFFFFFFFF  # 32-bit BSS trailer field
MAX_SECTION_ALIGN = 4096  # one page
# `version` is checked by _load_json_dict, so it's not listed here.
REQUIRED_KEYS = ("extern", "relocations", "sections", "symbols")
# SECTION_ORDER is layout order (text first so the entry point lands at PROGRAM_BASE),
# not alphabetical.
SECTION_ORDER: tuple[str, ...] = ("text", "rodata", "data")
UNKNOWN_SOURCE = "<unknown>"


@dataclass
class LinkLayout:
    """Result of laying out sections across all loaded objects.

    Each section dict in every object also gets a ``slot_address`` key set
    to the absolute address of that object's contribution to the section.
    Downstream consumers read it as
    ``object_payload["sections"][name]["slot_address"]``.
    """

    bss_size: int
    image: bytearray
    symbol_addresses: dict[str, int]


def _align_up(offset: int, alignment: int) -> int:
    """Round ``offset`` up to the next multiple of ``alignment``."""
    return (offset + alignment - 1) & ~(alignment - 1)


def _apply_relocations(
    *,
    base: int,
    image: bytearray,
    object_payloads: list[dict],
    symbol_addresses: dict[str, int],
) -> None:
    """Walk every relocation in every object and patch the image bytes.

    All validation (symbol resolution, rel32 displacement range, abs32
    address range) happens earlier in ``_validate_relocations``.  This
    pass is pure patching.
    """
    for object_payload in object_payloads:
        local_addresses = object_payload.get("local_addresses", {})
        for relocation in object_payload["relocations"]:
            section = object_payload["sections"][relocation["section"]]
            patch_address = section["slot_address"] + relocation["offset"]
            patch_offset_in_image = patch_address - base
            symbol_name = relocation["symbol"]
            # Object-local symbols (cc.py's _g_/_l_/_str_/_arr_ labels)
            # resolve from the owning object's per-file table first;
            # global symbols fall through to the cross-object table.
            symbol_address = local_addresses.get(symbol_name) or symbol_addresses[symbol_name]
            packed = (
                struct.pack("<i", symbol_address - (patch_address + 4))
                if relocation["type"] == "rel32"
                else struct.pack("<I", symbol_address)
            )
            image[patch_offset_in_image : patch_offset_in_image + 4] = packed


def _concatenate_section(
    *,
    base: int,
    image: bytearray,
    object_payloads: list[dict],
    section_name: str,
) -> None:
    """Pad ``image`` to the section's alignment, then append every object's slice.

    Each contributing section dict is augmented with ``slot_address``
    (absolute address of its bytes in the final image).
    """
    contributors = [payload["sections"][section_name] for payload in object_payloads if section_name in payload["sections"]]
    if not contributors:
        return
    section_alignment = max(DEFAULT_ALIGN[section_name], *(section["align"] for section in contributors))
    padded = _align_up(len(image), section_alignment)
    image.extend(b"\x00" * (padded - len(image)))
    for section in contributors:
        section["slot_address"] = base + len(image)
        image.extend(section["bytes"])


def _lay_out_sections(*, base: int, object_payloads: list[dict]) -> LinkLayout:
    """Lay out sections across multiple objects.

    Within each section, objects' bytes are concatenated in input order.
    Each section is aligned to ``max(DEFAULT_ALIGN[name], per-object aligns)``.
    Each contributing section dict is augmented with ``slot_address``
    (absolute address of its slot) for downstream relocation patching.
    """
    image = bytearray()
    bss_size = 0

    for section_name in SECTION_ORDER:
        _concatenate_section(base=base, image=image, object_payloads=object_payloads, section_name=section_name)

    # BSS sits immediately after the trailer (no bytes in image).
    bss_cursor = base + len(image) + BSS_TRAILER_SIZE
    for object_payload in object_payloads:
        if "bss" not in object_payload["sections"]:
            continue
        bss_section = object_payload["sections"]["bss"]
        bss_section["slot_address"] = bss_cursor
        bss_cursor += bss_section["size"]
        bss_size += bss_section["size"]

    symbol_addresses: dict[str, int] = {}
    # Records the object_payload that first defined each global symbol so
    # duplicate-global errors can name both contributing sources.
    symbol_origin: dict[str, dict] = {}
    for object_payload in object_payloads:
        # Per-object local-symbol table.  cc.py marks every in-translation-
        # unit data label (``_g_x`` / ``_l_x`` / ``_str_N`` / ``_arr_N``) as
        # ``binding: local`` because they should not collide with same-named
        # symbols in other objects.  Relocations within an object still need
        # to find their targets, so the linker resolves each relocation
        # against the owning object's local table first and falls back to
        # the global table second.
        local_addresses: dict[str, int] = {}
        for symbol_name, info in object_payload["symbols"].items():
            section = object_payload["sections"][info["section"]]
            address = section["slot_address"] + info["offset"]
            if info["binding"] != "global":
                local_addresses[symbol_name] = address
                continue
            if symbol_name in symbol_addresses:
                previous_source = symbol_origin[symbol_name]["source"]
                sys.exit(f"ccld: symbol {symbol_name!r} defined more than once: {previous_source} and {object_payload['source']}")
            symbol_addresses[symbol_name] = address
            symbol_origin[symbol_name] = object_payload
        object_payload["local_addresses"] = local_addresses

    return LinkLayout(bss_size=bss_size, image=image, symbol_addresses=symbol_addresses)


def _load_ccar(path: Path, /) -> list[dict]:
    """Read a `.ccar` manifest and return its validated members list.

    Each member dict has keys ``file`` (basename), ``provides`` (list of
    globally-defined symbol names), and ``path`` (resolved absolute Path
    set by ``_validate_ccar_member``).  Member content is loaded lazily
    by :func:`_pull_archive_members`.
    """
    manifest = _load_json_dict(expected_version=CCAR_VERSION, format_name="ccar", path=path)
    _validate_ccar_shape(manifest=manifest, path=path)
    return manifest["members"]


def _load_ccobj(path: Path, /) -> dict:
    """Read a `.ccobj` JSON file and validate its schema."""
    payload = _load_json_dict(expected_version=CCOBJ_VERSION, format_name="ccobj", path=path)
    missing = [key for key in REQUIRED_KEYS if key not in payload]
    if missing:
        sys.exit(f"ccld: {path}: missing required key(s): {', '.join(missing)}")
    _validate_ccobj_shape(path=path, payload=payload)
    return payload


def _load_json_dict(*, expected_version: int, format_name: str, path: Path) -> dict:
    """Read a JSON file that should be a ``{version: N, ...}`` object."""
    try:
        with path.open(encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as error:
        sys.exit(f"ccld: {path}: invalid {format_name} JSON ({error})")
    if not isinstance(payload, dict):
        sys.exit(f"ccld: {path}: top-level value is not a JSON object")
    if (actual_version := payload.get("version")) != expected_version:
        sys.exit(f"ccld: {path}: unsupported .{format_name} version {actual_version!r} (expected {expected_version})")
    return payload


def _pull_archive_members(
    *,
    archive_members: list[dict],
    object_payloads: list[dict],
) -> None:
    """Pull archive members into ``object_payloads`` until reaching fixed point.

    Modifies ``object_payloads`` in place.  A member is pulled in when
    at least one of its ``provides`` symbols matches an unresolved
    extern from the currently-loaded objects.  Pulling in a member can
    introduce new externs, so iterate until either every extern is
    satisfied or no archive can supply a missing symbol — in the latter
    case ``_resolve_symbols`` later produces the canonical error.
    """
    pulled_files: set[Path] = set()
    while unresolved := _unresolved_externs(object_payloads=object_payloads):
        pulled_this_pass = False
        for member in archive_members:
            if not member["provides"].intersection(unresolved):
                continue
            member_path = member["path"]
            if member_path in pulled_files:
                continue
            object_payloads.append(_load_ccobj(member_path))
            pulled_files.add(member_path)
            pulled_this_pass = True
        if not pulled_this_pass:
            # No archive can satisfy any remaining unresolved extern.
            # Let ``_resolve_symbols`` produce the canonical error.
            break


def _resolve_symbols(*, object_payloads: list[dict]) -> None:
    """Validate every extern has a defining global symbol.

    Duplicate-global detection happens earlier in ``_lay_out_sections``;
    this pass only verifies each object's extern list resolves against
    the union of all defined globals.
    """
    unresolved_set = _unresolved_externs(object_payloads=object_payloads)
    if not unresolved_set:
        return
    lines = ["ccld: unresolved external symbols:"]
    for object_payload in object_payloads:
        lines.extend(
            f"  - {extern_name} (referenced by {object_payload['source']})"
            for extern_name in object_payload["extern"]
            if extern_name in unresolved_set
        )
    sys.exit("\n".join(lines))


def _unresolved_externs(*, object_payloads: list[dict]) -> set[str]:
    """Return the set of extern names that no loaded object defines as global."""
    defined = {
        name for object_payload in object_payloads for name, info in object_payload["symbols"].items() if info["binding"] == "global"
    }
    return {extern_name for object_payload in object_payloads for extern_name in object_payload["extern"] if extern_name not in defined}


def _validate_ccar_member(*, index: int, manifest_directory: Path, member: dict, path: Path) -> None:
    """Validate the shape of a single archive-manifest member entry."""
    if not isinstance(member, dict):
        sys.exit(f"ccld: {path}: member #{index} must be an object")
    file_basename = member.get("file")
    if not isinstance(file_basename, str) or not file_basename:
        sys.exit(f"ccld: {path}: member #{index}: `file` must be a non-empty string")
    # Members must resolve to a sibling of the manifest — anything that
    # ends up in a subdirectory, the parent directory, or an absolute
    # path outside the manifest's directory is rejected.
    member_path = (manifest_directory / file_basename).resolve()
    if member_path.parent != manifest_directory:
        sys.exit(f"ccld: {path}: member #{index}: `file` {file_basename!r} must live in the manifest's directory ({manifest_directory})")
    if not member_path.is_file():
        sys.exit(f"ccld: {path}: member #{index}: `file` {file_basename!r} does not exist on disk")
    # Stash the resolved path so _pull_archive_members doesn't re-resolve.
    member["path"] = member_path
    provides = member.get("provides")
    if not isinstance(provides, list) or not all(isinstance(name, str) and name for name in provides):
        sys.exit(f"ccld: {path}: member #{index}: `provides` must be a list of non-empty strings")
    if len(set(provides)) != len(provides):
        sys.exit(f"ccld: {path}: member #{index}: `provides` contains duplicate names")
    # Replace the list with a frozenset so _pull_archive_members can call
    # `.intersection(unresolved)` directly each pass without rebuilding.
    member["provides"] = frozenset(provides)


def _validate_ccar_shape(*, manifest: dict, path: Path) -> None:
    """Validate the inner shape of a parsed `.ccar` manifest."""
    members = manifest.get("members")
    if not isinstance(members, list):
        sys.exit(f"ccld: {path}: `members` must be a list")
    manifest_directory = path.parent.resolve()
    seen_files: set[str] = set()
    for index, member in enumerate(members):
        _validate_ccar_member(index=index, manifest_directory=manifest_directory, member=member, path=path)
        if member["file"] in seen_files:
            sys.exit(f"ccld: {path}: member #{index}: duplicate file {member['file']!r}")
        seen_files.add(member["file"])


def _validate_ccobj_relocations(*, path: Path, relocations: object, section_sizes: dict[str, int]) -> None:
    """Validate the relocations list: shape of every entry + no duplicate patch sites."""
    if not isinstance(relocations, list):
        sys.exit(f"ccld: {path}: `relocations` must be a list")
    seen_patch_sites: set[tuple[str, int]] = set()
    for index, relocation in enumerate(relocations):
        _validate_relocation_entry(index=index, path=path, relocation=relocation, section_sizes=section_sizes)
        patch_site = (relocation["section"], relocation["offset"])
        if patch_site in seen_patch_sites:
            sys.exit(f"ccld: {path}: relocation #{index}: duplicate patch site at section .{patch_site[0]} offset {patch_site[1]}")
        seen_patch_sites.add(patch_site)


def _validate_ccobj_shape(*, path: Path, payload: dict) -> None:
    """Validate the inner shape of every field in a parsed `.ccobj` payload.

    Top-level keys and version are already checked by ``_load_ccobj``
    (via ``_load_json_dict``); this pass verifies each section, symbol,
    extern, and relocation entry has the right types and ranges so later
    consumers can rely on the data without further defensive checks.
    """
    if "source" in payload and not isinstance(payload["source"], str):
        sys.exit(f"ccld: {path}: `source` must be a string when present")
    # Fill in a default so consumers can read payload["source"] directly.
    payload.setdefault("source", UNKNOWN_SOURCE)

    sections = payload["sections"]
    if not isinstance(sections, dict):
        sys.exit(f"ccld: {path}: `sections` must be an object")
    section_sizes: dict[str, int] = {}
    for section_name, section in sections.items():
        _validate_section_entry(path=path, section=section, section_name=section_name)
        # _validate_section_entry replaced base64 string with bytes for non-bss.
        section_sizes[section_name] = section["size"] if section_name == "bss" else len(section["bytes"])

    symbols = payload["symbols"]
    if not isinstance(symbols, dict):
        sys.exit(f"ccld: {path}: `symbols` must be an object")
    for symbol_name, info in symbols.items():
        if not symbol_name:
            sys.exit(f"ccld: {path}: symbol name must be a non-empty string")
        _validate_symbol_entry(info=info, path=path, section_sizes=section_sizes, symbol_name=symbol_name)

    extern = payload["extern"]
    if not isinstance(extern, list) or not all(isinstance(name, str) and name for name in extern):
        sys.exit(f"ccld: {path}: `extern` must be a list of non-empty strings")
    if len(set(extern)) != len(extern):
        sys.exit(f"ccld: {path}: `extern` contains duplicate names")

    _validate_ccobj_relocations(path=path, relocations=payload["relocations"], section_sizes=section_sizes)


def _validate_relocation_entry(*, index: int, path: Path, relocation: dict, section_sizes: dict[str, int]) -> None:
    """Validate the shape of a single relocation entry."""
    if not isinstance(relocation, dict):
        sys.exit(f"ccld: {path}: relocation #{index} must be an object")
    section_name = relocation.get("section")
    if section_name not in KNOWN_SECTIONS:
        sys.exit(f"ccld: {path}: relocation #{index}: `section` must be one of {KNOWN_SECTIONS}")
    if section_name not in section_sizes:
        sys.exit(f"ccld: {path}: relocation #{index}: references undefined section .{section_name}")
    offset = relocation.get("offset")
    if not isinstance(offset, int) or offset < 0:
        sys.exit(f"ccld: {path}: relocation #{index}: `offset` must be a non-negative int")
    # rel32 / abs32 patches are 4 bytes; the whole patch site must fit in the section.
    if offset + 4 > section_sizes[section_name]:
        sys.exit(f"ccld: {path}: relocation #{index}: offset {offset}+4 exceeds section .{section_name} size {section_sizes[section_name]}")
    symbol = relocation.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        sys.exit(f"ccld: {path}: relocation #{index}: `symbol` must be a non-empty string")
    if (relocation_type := relocation.get("type")) not in KNOWN_RELOCATION_TYPES:
        sys.exit(f"ccld: {path}: relocation #{index}: unknown type {relocation_type!r} (expected one of {KNOWN_RELOCATION_TYPES})")


def _validate_relocations(*, object_payloads: list[dict], symbol_addresses: dict[str, int]) -> None:
    """Post-layout pass: confirm every relocation can be applied.

    Verifies the relocation's symbol resolved to an address and, for
    rel32, that the displacement fits in a signed 32-bit int.  Runs
    before any patching so a failure leaves the output untouched.
    """
    for object_payload in object_payloads:
        local_addresses = object_payload.get("local_addresses", {})
        for relocation in object_payload["relocations"]:
            symbol = relocation["symbol"]
            symbol_address = local_addresses.get(symbol)
            if symbol_address is None:
                if symbol not in symbol_addresses:
                    sys.exit(f"ccld: {object_payload['source']}: relocation references unknown symbol {symbol!r}")
                symbol_address = symbol_addresses[symbol]
            if relocation["type"] != "rel32":
                continue
            section = object_payload["sections"][relocation["section"]]
            patch_address = section["slot_address"] + relocation["offset"]
            displacement = symbol_address - (patch_address + 4)
            if displacement < -(1 << 31) or displacement >= (1 << 31):
                sys.exit(f"ccld: {symbol!r} too far for rel32 (displacement {displacement})")


def _validate_section_entry(*, path: Path, section: dict, section_name: str) -> None:
    """Validate the shape of a single section entry."""
    if section_name not in KNOWN_SECTIONS:
        sys.exit(f"ccld: {path}: unknown section .{section_name}")
    if not isinstance(section, dict):
        sys.exit(f"ccld: {path}: section .{section_name} must be an object")
    align = section.get("align")
    # Power-of-two cap matches the page size (and `_align_up`'s bit-twiddle
    # assumes power-of-two alignment).
    if not isinstance(align, int) or align <= 0 or align > MAX_SECTION_ALIGN or align & (align - 1):
        sys.exit(f"ccld: {path}: section .{section_name}: `align` must be a power of two in 1..{MAX_SECTION_ALIGN}")
    if section_name == "bss":
        size = section.get("size")
        # The BSS trailer encodes size as a 32-bit field; values above this would silently truncate.
        if not isinstance(size, int) or size < 0 or size > MAX_BSS_SIZE:
            sys.exit(f"ccld: {path}: section .bss: `size` must be in 0..{MAX_BSS_SIZE}")
        return
    encoded = section.get("bytes")
    if not isinstance(encoded, str):
        sys.exit(f"ccld: {path}: section .{section_name}: `bytes` must be a base64 string")
    try:
        # Decode once here and replace the string with the resulting bytes so
        # downstream consumers can read section["bytes"] directly.
        section["bytes"] = base64.b64decode(encoded, validate=True)
    except binascii.Error as error:
        sys.exit(f"ccld: {path}: section .{section_name}: invalid base64 `bytes` ({error})")


def _validate_symbol_entry(*, info: dict, path: Path, section_sizes: dict[str, int], symbol_name: str) -> None:
    """Validate the shape of a single symbol entry."""
    if not isinstance(info, dict):
        sys.exit(f"ccld: {path}: symbol {symbol_name!r} must be an object")
    if info.get("binding") not in KNOWN_BINDINGS:
        sys.exit(f"ccld: {path}: symbol {symbol_name!r}: `binding` must be 'global' or 'local'")
    section_name = info.get("section")
    if section_name not in KNOWN_SECTIONS:
        sys.exit(f"ccld: {path}: symbol {symbol_name!r}: `section` must be one of {KNOWN_SECTIONS}")
    if section_name not in section_sizes:
        sys.exit(f"ccld: {path}: symbol {symbol_name!r}: references undefined section .{section_name}")
    offset = info.get("offset")
    if not isinstance(offset, int) or offset < 0:
        sys.exit(f"ccld: {path}: symbol {symbol_name!r}: `offset` must be a non-negative int")
    # End-of-section labels are legal (offset == size); past-the-end is not.
    if offset > section_sizes[section_name]:
        sys.exit(
            f"ccld: {path}: symbol {symbol_name!r}: offset {offset} exceeds section .{section_name} size {section_sizes[section_name]}"
        )


def main() -> int:
    """Parse arguments and link the given inputs into a flat binary."""
    parser = argparse.ArgumentParser(
        description="Link .ccobj files (and .ccar archives) into a flat binary.",
    )
    parser.add_argument(
        "--base",
        default="0x08048000",
        help="Load address of the output binary (hex).  Default: 0x08048000 (PROGRAM_BASE).",
    )
    parser.add_argument(
        "--emit-map",
        type=Path,
        help="Optional output path for a JSON symbol map (debug aid).",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output path for the flat binary.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="One or more .ccobj or .ccar files.  Positional order determines section layout.",
    )
    arguments = parser.parse_args()

    # Validate CLI inputs before doing any disk I/O on the .ccobj / .ccar files.
    try:
        base = int(arguments.base, 0)
    except ValueError:
        sys.exit(f"ccld: --base {arguments.base!r} is not a valid integer literal")
    if base < 0 or base > MAX_BSS_SIZE:
        sys.exit(f"ccld: --base {base:#x} must be in 0..{MAX_BSS_SIZE:#x}")

    object_payloads: list[dict] = []
    archive_members: list[dict] = []
    for input_path in arguments.inputs:
        if input_path.suffix == ".ccobj":
            object_payloads.append(_load_ccobj(input_path))
        elif input_path.suffix == ".ccar":
            archive_members.extend(_load_ccar(input_path))
        else:
            sys.exit(f"ccld: {input_path}: unsupported input type (expected .ccobj or .ccar)")

    _pull_archive_members(archive_members=archive_members, object_payloads=object_payloads)

    layout = _lay_out_sections(base=base, object_payloads=object_payloads)
    # Global "fits in 4 GB" check: every symbol address is in
    # [base, base + len(image) + BSS_TRAILER_SIZE + bss_size).  If that range
    # fits in [0, 2^32), no abs32 patch can overflow.
    total_bytes = len(layout.image) + BSS_TRAILER_SIZE + layout.bss_size
    if base + total_bytes > MAX_BSS_SIZE + 1:
        sys.exit(f"ccld: linked image (base {base:#x} + {total_bytes} bytes) overflows 4 GB address space")
    _resolve_symbols(object_payloads=object_payloads)
    _validate_relocations(
        object_payloads=object_payloads,
        symbol_addresses=layout.symbol_addresses,
    )
    _apply_relocations(
        base=base,
        image=layout.image,
        object_payloads=object_payloads,
        symbol_addresses=layout.symbol_addresses,
    )
    layout.image += struct.pack("<IH", layout.bss_size, BSS_MAGIC32)

    arguments.output.write_bytes(bytes(layout.image))
    if arguments.emit_map is not None:
        with arguments.emit_map.open("w", encoding="utf-8") as file:
            json.dump({"symbols": layout.symbol_addresses}, file, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
