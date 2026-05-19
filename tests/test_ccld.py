#!/usr/bin/env python3
"""Tests for tools/ccld.py (cc.py object-file linker)."""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import sys
from pathlib import Path

BSS_MAGIC32 = 0xB032
PROGRAM_BASE = 0x08048000
REPO_ROOT = Path(__file__).resolve().parent.parent

CCLD = REPO_ROOT / "tools" / "ccld.py"


def _write_ccobj(
    path: Path,
    /,
    *,
    bss_size: int = 0,
    data_bytes: bytes = b"",
    extern: list[str] | None = None,
    omit_source: bool = False,
    relocations: list[dict] | None = None,
    rodata_bytes: bytes = b"",
    symbols: dict[str, dict] | None = None,
    text_bytes: bytes = b"",
) -> None:
    """Materialize a hand-crafted .ccobj at ``path``."""
    sections: dict[str, dict] = {}
    if text_bytes:
        sections["text"] = {
            "align": 16,
            "bytes": base64.b64encode(text_bytes).decode("ascii"),
        }
    if rodata_bytes:
        sections["rodata"] = {
            "align": 4,
            "bytes": base64.b64encode(rodata_bytes).decode("ascii"),
        }
    if data_bytes:
        sections["data"] = {
            "align": 4,
            "bytes": base64.b64encode(data_bytes).decode("ascii"),
        }
    if bss_size > 0:
        sections["bss"] = {"align": 4, "size": bss_size}
    payload: dict = {
        "extern": extern or [],
        "relocations": relocations or [],
        "sections": sections,
        "symbols": symbols or {},
        "version": 1,
    }
    if not omit_source:
        payload["source"] = str(path)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)


def test_end_to_end_c_to_flat_binary(tmp_path: Path) -> None:
    """Full pipeline smoke: C source through cc.py --object → nasm → pack-ccobj → ccld.

    Exercises the contract between PR 1's producer (`cc.py --object` +
    `pack-ccobj`) and PR 2's linker.  A trivial C source that calls an
    extern ``die`` is compiled, assembled, packed, then linked against a
    hand-written ``die.ccobj`` stub.  The linked flat binary must carry
    the BSS trailer and the symbol map must place ``main`` at
    ``PROGRAM_BASE`` with ``die`` somewhere past it.
    """
    cc = REPO_ROOT / "cc.py"
    source = tmp_path / "calls_die.c"
    source.write_text(
        'extern void die(const char *message);\nint main() {\n    die("oops");\n    return 1;\n}\n',
        encoding="utf-8",
    )
    assembly = tmp_path / "calls_die.asm"
    binary = tmp_path / "calls_die.bin"
    listing = tmp_path / "calls_die.lst"
    main_object = tmp_path / "calls_die.ccobj"
    subprocess.run(
        [sys.executable, str(cc), "--bits", "32", "--object", str(source), str(assembly)],
        check=True,
    )
    subprocess.run(
        [
            "nasm",
            "-f",
            "bin",
            "-i",
            str(REPO_ROOT / "src" / "include") + "/",
            "-l",
            str(listing),
            "-o",
            str(binary),
            str(assembly),
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(cc), "pack-ccobj", str(binary), str(listing), str(main_object)],
        check=True,
    )

    # Hand-written stub: defines `die` as a single `ret` so the linker can
    # resolve the extern without needing the real runtime archive.
    die_object = tmp_path / "die.ccobj"
    _write_ccobj(
        die_object,
        symbols={"die": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=bytes([0xC3]),
    )

    output = tmp_path / "linked.bin"
    map_path = tmp_path / "linked.map"
    subprocess.run(
        [
            sys.executable,
            str(CCLD),
            "--emit-map",
            str(map_path),
            "--output",
            str(output),
            str(main_object),
            str(die_object),
        ],
        check=True,
    )

    # BSS trailer present at the tail of the image (size may be non-zero
    # because cc.py reserves BSS for string literals — the value just has
    # to be decodable, not a specific number).
    image = output.read_bytes()
    bss_size, magic = struct.unpack_from("<IH", image, len(image) - 6)
    assert magic == BSS_MAGIC32
    assert bss_size >= 0

    # main lands at PROGRAM_BASE; die is pulled in past main.
    with map_path.open(encoding="utf-8") as file:
        map_data = json.load(file)
    assert map_data["symbols"]["main"] == PROGRAM_BASE
    assert map_data["symbols"]["die"] > PROGRAM_BASE


def test_end_to_end_globals_relocate_across_sections(tmp_path: Path) -> None:
    """Full pipeline: cross-section abs32 refs survive to the linked image.

    cc.py --object lays initialized globals into ``.data``, strings
    into ``.rodata``, and zero-init globals into ``.bss``, then emits
    ``mov [_g_x], eax`` / ``push _str_0`` / ``mov eax, [_g_x]``
    instructions in ``.text`` whose abs32 operands NASM leaves as
    bracketed placeholders.  pack-ccobj recognises the bracket form
    and emits abs32 relocations.  ccld places each section, resolves
    every relocation against the per-object local symbol table, and
    patches the 4-byte placeholders with the final absolute address.

    This test exercises the contract end-to-end.  The resulting flat
    binary's `.text` bytes must hold patched (non-zero) abs32 operands
    pointing at addresses inside the linked image's data range.
    """
    cc = REPO_ROOT / "cc.py"
    source = tmp_path / "globals.c"
    source.write_text('int g_init = 42;\nint g_zero;\nint main() {\n    g_zero = g_init;\n    printf("%d\\n", g_zero);\n    return 0;\n}\n')
    assembly = tmp_path / "globals.asm"
    binary = tmp_path / "globals.bin"
    listing = tmp_path / "globals.lst"
    obj = tmp_path / "globals.ccobj"
    subprocess.run(
        [sys.executable, str(cc), "--bits", "32", "--object", str(source), str(assembly)],
        check=True,
    )
    subprocess.run(
        [
            "nasm",
            "-f",
            "bin",
            "-i",
            str(REPO_ROOT / "src" / "include") + "/",
            "-l",
            str(listing),
            "-o",
            str(binary),
            str(assembly),
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(cc), "pack-ccobj", str(binary), str(listing), str(obj)],
        check=True,
    )

    output = tmp_path / "linked.bin"
    map_path = tmp_path / "linked.map"
    subprocess.run(
        [
            sys.executable,
            str(CCLD),
            "--emit-map",
            str(map_path),
            "--output",
            str(output),
            str(obj),
        ],
        check=True,
    )

    image = output.read_bytes()
    # BSS trailer for ``_g_g_zero`` (one int).
    bss_size, magic = struct.unpack_from("<IH", image, len(image) - 6)
    assert magic == BSS_MAGIC32
    assert bss_size == 4

    # main is the only global; data/string symbols are locals and
    # don't appear in the public map.
    with map_path.open(encoding="utf-8") as file:
        symbols = json.load(file)["symbols"]
    assert symbols["main"] == PROGRAM_BASE

    # The very first instruction emitted into main is
    # ``mov eax, [_g_g_init]`` — encoded ``A1 <abs32>``.  Its abs32
    # operand sits at image offset 1; after relocation that abs32
    # must point inside the linked image.  An unpatched relocation
    # would leave four zero bytes there.
    assert image[0] == 0xA1, image[:6].hex()
    operand = struct.unpack_from("<I", image, 1)[0]
    assert PROGRAM_BASE <= operand < PROGRAM_BASE + len(image), hex(operand)


def test_help_prints_usage() -> None:
    """`tools/ccld.py --help` exits 0 and prints usage."""
    result = subprocess.run(
        [sys.executable, str(CCLD), "--help"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert "--output" in result.stdout
    assert "--base" in result.stdout


def test_link_all_sections_single_object(tmp_path: Path) -> None:
    """Single object with text + rodata + data + bss lays out correctly."""
    object_payload = tmp_path / "all_sections.ccobj"
    text = b"\x90" * 17  # 17 bytes: forces rodata to start at offset 20 (next 4-aligned)
    rodata = b"hello\x00"  # 6 bytes: forces data to start at offset 28 (next 4-aligned)
    data = b"\x2a\x00\x00\x00"  # one dword = 42
    _write_ccobj(
        object_payload,
        bss_size=64,
        data_bytes=data,
        rodata_bytes=rodata,
        text_bytes=text,
    )
    output = tmp_path / "all.bin"
    subprocess.run(
        [sys.executable, str(CCLD), "--output", str(output), str(object_payload)],
        check=True,
    )

    image = output.read_bytes()
    # Layout: text @ 0; rodata @ 20 (pad 3 bytes); data @ 28 (pad 2 bytes); trailer @ 32.
    assert image[0:17] == text
    assert image[17:20] == b"\x00\x00\x00"
    assert image[20:26] == rodata
    assert image[26:28] == b"\x00\x00"
    assert image[28:32] == data
    bss_size, magic = struct.unpack_from("<IH", image, 32)
    assert bss_size == 64
    assert magic == BSS_MAGIC32
    assert len(image) == 38


def test_link_applies_abs32_relocation(tmp_path: Path) -> None:
    """An abs32 reloc patches in the absolute address of the symbol."""
    a = tmp_path / "a.ccobj"
    b = tmp_path / "b.ccobj"
    # mov eax, [errno] = A1 <abs32>; patch site at offset 1.
    a_text = bytes([0xA1, 0x00, 0x00, 0x00, 0x00, 0xC3])
    _write_ccobj(
        a,
        extern=["errno"],
        relocations=[
            {"section": "text", "offset": 1, "symbol": "errno", "type": "abs32"},
        ],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=a_text,
    )
    _write_ccobj(
        b,
        bss_size=4,
        symbols={"errno": {"section": "bss", "offset": 0, "binding": "global"}},
    )
    output = tmp_path / "abs32.bin"
    subprocess.run(
        [sys.executable, str(CCLD), "--output", str(output), str(a), str(b)],
        check=True,
    )

    image = output.read_bytes()
    patched = struct.unpack_from("<I", image, 1)[0]
    # errno is in BSS, which lives at PROGRAM_BASE + len(image_excluding_trailer) + 6.
    # a_text has 6 bytes of text; no other sections; trailer adds 6 bytes; BSS starts at +12.
    assert patched == PROGRAM_BASE + len(a_text) + 6


def test_link_applies_rel32_relocation(tmp_path: Path) -> None:
    """A rel32 reloc patches in the signed displacement to the symbol.

    Object A defines ``main`` and calls ``die`` (a 5-byte CALL: E8 00 00 00 00,
    with the rel32 placeholder at offset 1).  Object B defines ``die``.
    """
    a = tmp_path / "a.ccobj"
    b = tmp_path / "b.ccobj"
    a_text = bytes([0xE8, 0x00, 0x00, 0x00, 0x00, 0xC3])  # call rel32; ret
    _write_ccobj(
        a,
        extern=["die"],
        relocations=[
            {"section": "text", "offset": 1, "symbol": "die", "type": "rel32"},
        ],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=a_text,
    )
    b_text = bytes([0xCC, 0xCC, 0xC3])  # int3 int3 ret
    _write_ccobj(
        b,
        symbols={"die": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b_text,
    )
    output = tmp_path / "relocated.bin"
    subprocess.run(
        [sys.executable, str(CCLD), "--output", str(output), str(a), str(b)],
        check=True,
    )

    image = output.read_bytes()
    # main @ PROGRAM_BASE; call patch site @ PROGRAM_BASE + 1; next insn @ PROGRAM_BASE + 5.
    # die @ PROGRAM_BASE + len(a_text) = PROGRAM_BASE + 6.
    # Displacement = (PROGRAM_BASE + 6) - (PROGRAM_BASE + 1 + 4) = 1.
    displacement = struct.unpack_from("<i", image, 1)[0]
    assert displacement == 1
    # Bytes around the patch unchanged.
    assert image[0] == 0xE8
    assert image[5] == 0xC3


def test_link_concatenates_text_across_objects(tmp_path: Path) -> None:
    """Two objects' text bytes are concatenated in command-line order.

    Object A's symbol stays at offset 0 of text; object B's symbol gets
    its offset bumped by A's text length.
    """
    a = tmp_path / "a.ccobj"
    b = tmp_path / "b.ccobj"
    a_text = bytes([0x90, 0x90, 0xC3])  # 3 bytes
    b_text = bytes([0xCC, 0xCC])  # 2 bytes
    _write_ccobj(
        a,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=a_text,
    )
    _write_ccobj(
        b,
        symbols={"helper": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b_text,
    )
    output = tmp_path / "concat.bin"
    map_path = tmp_path / "concat.map"
    subprocess.run(
        [
            sys.executable,
            str(CCLD),
            "--emit-map",
            str(map_path),
            "--output",
            str(output),
            str(a),
            str(b),
        ],
        check=True,
    )

    image = output.read_bytes()
    assert image[: len(a_text)] == a_text
    assert image[len(a_text) : len(a_text) + len(b_text)] == b_text
    with map_path.open(encoding="utf-8") as file:
        map_data = json.load(file)
    assert map_data["symbols"]["main"] == PROGRAM_BASE
    assert map_data["symbols"]["helper"] == PROGRAM_BASE + len(a_text)


def test_link_duplicate_local_symbol_is_fine(tmp_path: Path) -> None:
    """Two objects can define the same local symbol — they're object-private."""
    a = tmp_path / "a.ccobj"
    b = tmp_path / "b.ccobj"
    _write_ccobj(
        a,
        symbols={
            "main": {"section": "text", "offset": 0, "binding": "global"},
            "tmp": {"section": "text", "offset": 0, "binding": "local"},
        },
        text_bytes=b"\xc3",
    )
    _write_ccobj(
        b,
        symbols={
            "helper": {"section": "text", "offset": 0, "binding": "global"},
            "tmp": {"section": "text", "offset": 0, "binding": "local"},
        },
        text_bytes=b"\xc3",
    )
    subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(a), str(b)],
        check=True,
    )


def test_link_pulls_archive_member_on_demand(tmp_path: Path) -> None:
    """An unresolved extern triggers archive member pull-in.

    ``main.ccobj`` calls ``die``; the archive contains both ``die.ccobj``
    and ``errno.ccobj``.  Only ``die`` is referenced, so ``errno`` must
    NOT end up in the linked output.
    """
    main_object = tmp_path / "main.ccobj"
    _write_ccobj(
        main_object,
        extern=["die"],
        relocations=[{"section": "text", "offset": 1, "symbol": "die", "type": "rel32"}],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=bytes([0xE8, 0, 0, 0, 0, 0xC3]),
    )
    die_object = tmp_path / "die.ccobj"
    _write_ccobj(
        die_object,
        symbols={"die": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=bytes([0xC3]),
    )
    errno_object = tmp_path / "errno.ccobj"
    _write_ccobj(
        errno_object,
        bss_size=4,
        symbols={"errno": {"section": "bss", "offset": 0, "binding": "global"}},
    )
    archive = tmp_path / "runtime.ccar"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "ccar.py"),
            "--output",
            str(archive),
            str(die_object),
            str(errno_object),
        ],
        check=True,
    )

    output = tmp_path / "linked.bin"
    map_path = tmp_path / "linked.map"
    subprocess.run(
        [
            sys.executable,
            str(CCLD),
            "--emit-map",
            str(map_path),
            "--output",
            str(output),
            str(main_object),
            str(archive),
        ],
        check=True,
    )

    with map_path.open(encoding="utf-8") as file:
        map_data = json.load(file)
    # main and die are pulled in; errno is not (no extern referenced it).
    assert "main" in map_data["symbols"]
    assert "die" in map_data["symbols"]
    assert "errno" not in map_data["symbols"]


def test_link_records_bss_symbol_addresses(tmp_path: Path) -> None:
    """BSS symbols get addresses past end of image (excluding trailer).

    Verifies the linker resolved ``scratch`` to PROGRAM_BASE + text_size + 6
    (trailer) and stored it in the eventual symbol map.  Forced via
    --emit-map for inspection.
    """
    object_payload = tmp_path / "with_bss.ccobj"
    text = b"\x90\x90\xc3"  # 3 bytes
    _write_ccobj(
        object_payload,
        bss_size=128,
        symbols={
            "main": {"section": "text", "offset": 0, "binding": "global"},
            "scratch": {"section": "bss", "offset": 0, "binding": "global"},
        },
        text_bytes=text,
    )
    output = tmp_path / "out.bin"
    map_path = tmp_path / "out.map"
    subprocess.run(
        [
            sys.executable,
            str(CCLD),
            "--emit-map",
            str(map_path),
            "--output",
            str(output),
            str(object_payload),
        ],
        check=True,
    )

    with map_path.open(encoding="utf-8") as file:
        map_data = json.load(file)
    # text starts at PROGRAM_BASE; main at offset 0 in text.
    assert map_data["symbols"]["main"] == PROGRAM_BASE
    # BSS starts immediately after the trailer (text=3, trailer=6, so BSS @ image_end = base+9).
    assert map_data["symbols"]["scratch"] == PROGRAM_BASE + 3 + 6


def test_link_rejects_base_negative(tmp_path: Path) -> None:
    """A negative `--base` is a hard error."""
    object_payload = tmp_path / "x.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--base", "-1", "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "--base" in result.stderr


def test_link_rejects_base_not_an_integer(tmp_path: Path) -> None:
    """A `--base` that doesn't parse as an int literal is a hard error."""
    object_payload = tmp_path / "x.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--base", "nope", "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "--base" in result.stderr
    assert "nope" in result.stderr


def test_link_rejects_duplicate_global_symbol(tmp_path: Path) -> None:
    """Two objects defining the same global symbol is a hard error."""
    a = tmp_path / "a.ccobj"
    b = tmp_path / "b.ccobj"
    _write_ccobj(
        a,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    _write_ccobj(
        b,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(a), str(b)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "main" in result.stderr
    assert "more than once" in result.stderr.lower() or "multiple" in result.stderr.lower() or "duplicate" in result.stderr.lower()


def test_link_rejects_duplicate_global_symbol_without_source(tmp_path: Path) -> None:
    """Duplicate-global is caught even when both objects omit the ``source`` field.

    Regression: an earlier implementation compared ``source`` strings to
    detect duplicates.  When both inputs lacked ``source``, both looked
    like ``"<unknown>"`` and the duplicate was silently lost.
    """
    a = tmp_path / "a.ccobj"
    b = tmp_path / "b.ccobj"
    _write_ccobj(
        a,
        omit_source=True,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    _write_ccobj(
        b,
        omit_source=True,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(a), str(b)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0, f"expected duplicate-global to be rejected; got returncode {result.returncode}, stderr={result.stderr!r}"
    assert "main" in result.stderr


def test_link_rejects_image_overflows_address_space(tmp_path: Path) -> None:
    """Base + total image size overflowing 2^32 is a hard error."""
    object_payload = tmp_path / "huge.ccobj"
    # bss size of 2^32-1 + default base 0x08048000 + trailer pushes well past 4 GB.
    _write_ccobj(
        object_payload,
        bss_size=0xFFFFFFFF,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "overflows" in result.stderr
    assert "4 GB" in result.stderr


def test_link_rejects_unresolved_extern(tmp_path: Path) -> None:
    """An extern symbol with no defining object is a hard error."""
    object_payload = tmp_path / "needs_die.ccobj"
    _write_ccobj(
        object_payload,
        extern=["die", "abort"],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    # Both unresolved symbols should appear in the error.
    assert "die" in result.stderr
    assert "abort" in result.stderr


def test_link_text_only_single_object(tmp_path: Path) -> None:
    """A single object with only text bytes produces text || zero-BSS trailer."""
    object_payload = tmp_path / "single.ccobj"
    text = bytes([0x90, 0x90, 0x90, 0xC3])  # nop nop nop ret
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=text,
    )
    output = tmp_path / "single.bin"
    subprocess.run(
        [sys.executable, str(CCLD), "--output", str(output), str(object_payload)],
        check=True,
    )

    image = output.read_bytes()
    assert image[: len(text)] == text
    bss_size, magic = struct.unpack_from("<IH", image, len(text))
    assert bss_size == 0
    assert magic == BSS_MAGIC32
    assert len(image) == len(text) + 6


def test_link_transitive_archive_pull_in(tmp_path: Path) -> None:
    """An archive member can introduce a new extern that another member satisfies.

    ``main.ccobj`` references ``die``; ``die.ccobj`` (in the archive)
    references ``_exit`` (also in the archive).  The linker must iterate
    to fixed point and pull in both.
    """
    main_object = tmp_path / "main.ccobj"
    _write_ccobj(
        main_object,
        extern=["die"],
        relocations=[{"section": "text", "offset": 1, "symbol": "die", "type": "rel32"}],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=bytes([0xE8, 0, 0, 0, 0, 0xC3]),
    )
    die_object = tmp_path / "die.ccobj"
    _write_ccobj(
        die_object,
        extern=["_exit"],
        relocations=[{"section": "text", "offset": 1, "symbol": "_exit", "type": "rel32"}],
        symbols={"die": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=bytes([0xE8, 0, 0, 0, 0, 0xC3]),
    )
    exit_object = tmp_path / "_exit.ccobj"
    _write_ccobj(
        exit_object,
        symbols={"_exit": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=bytes([0xC3]),
    )
    archive = tmp_path / "runtime.ccar"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "ccar.py"),
            "--output",
            str(archive),
            str(die_object),
            str(exit_object),
        ],
        check=True,
    )

    output = tmp_path / "linked.bin"
    map_path = tmp_path / "linked.map"
    subprocess.run(
        [
            sys.executable,
            str(CCLD),
            "--emit-map",
            str(map_path),
            "--output",
            str(output),
            str(main_object),
            str(archive),
        ],
        check=True,
    )

    with map_path.open(encoding="utf-8") as file:
        map_data = json.load(file)
    assert {"main", "die", "_exit"}.issubset(map_data["symbols"])


def test_load_rejects_ccar_duplicate_member_file(tmp_path: Path) -> None:
    """A manifest listing the same `file` twice is rejected at load."""
    object_payload = tmp_path / "x.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    archive = tmp_path / "lib.ccar"
    with archive.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "members": [
                    {"file": "x.ccobj", "provides": ["foo"]},
                    {"file": "x.ccobj", "provides": ["bar"]},
                ],
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload), str(archive)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "duplicate" in result.stderr.lower()


def test_load_rejects_ccar_member_directory_traversal(tmp_path: Path) -> None:
    """A manifest member whose `file` contains a path separator is rejected."""
    object_payload = tmp_path / "x.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    archive = tmp_path / "lib.ccar"
    with archive.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "members": [{"file": "../escaped.ccobj", "provides": ["foo"]}],
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload), str(archive)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "manifest's directory" in result.stderr


def test_load_rejects_ccar_member_missing_on_disk(tmp_path: Path) -> None:
    """A manifest pointing at a deleted .ccobj is rejected at load."""
    main_object = tmp_path / "main.ccobj"
    _write_ccobj(
        main_object,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    die_object = tmp_path / "die.ccobj"
    _write_ccobj(
        die_object,
        symbols={"die": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    archive = tmp_path / "runtime.ccar"
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "ccar.py"), "--output", str(archive), str(die_object)],
        check=True,
    )
    die_object.unlink()

    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(main_object), str(archive)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "die.ccobj" in result.stderr
    assert "does not exist" in result.stderr


def test_load_rejects_ccar_member_provides_not_list(tmp_path: Path) -> None:
    """A manifest member whose `provides` isn't a list of strings is rejected."""
    object_payload = tmp_path / "x.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    archive = tmp_path / "lib.ccar"
    with archive.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "members": [{"file": "x.ccobj", "provides": "foo"}],
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload), str(archive)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "provides" in result.stderr


def test_load_rejects_ccar_top_level_not_object(tmp_path: Path) -> None:
    """A `.ccar` whose JSON top-level isn't an object is rejected."""
    object_payload = tmp_path / "x.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    archive = tmp_path / "lib.ccar"
    with archive.open("w", encoding="utf-8") as file:
        json.dump([], file)
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload), str(archive)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "JSON object" in result.stderr


def test_load_rejects_extern_duplicate_names(tmp_path: Path) -> None:
    """An `extern` list with duplicate names is rejected at load."""
    object_payload = tmp_path / "x.ccobj"
    _write_ccobj(
        object_payload,
        extern=["die", "die"],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "duplicate" in result.stderr.lower()


def test_load_rejects_missing_keys(tmp_path: Path) -> None:
    """A `.ccobj` missing a required top-level key is a hard error."""
    object_payload = tmp_path / "missing.ccobj"
    with object_payload.open("w", encoding="utf-8") as file:
        json.dump({"sections": {}, "symbols": {}, "version": 1}, file)
    output = tmp_path / "out.bin"
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(output), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "extern" in result.stderr or "relocations" in result.stderr


def test_load_rejects_relocation_duplicate_patch_site(tmp_path: Path) -> None:
    """Two relocations at the same (section, offset) is rejected at load."""
    object_payload = tmp_path / "x.ccobj"
    _write_ccobj(
        object_payload,
        extern=["die", "_exit"],
        relocations=[
            {"section": "text", "offset": 1, "symbol": "die", "type": "rel32"},
            {"section": "text", "offset": 1, "symbol": "_exit", "type": "rel32"},
        ],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=bytes([0xE8, 0, 0, 0, 0, 0xC3]),
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "duplicate patch site" in result.stderr


def test_load_rejects_relocation_negative_offset(tmp_path: Path) -> None:
    """A relocation with a negative offset fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    _write_ccobj(
        object_payload,
        extern=["die"],
        relocations=[{"section": "text", "offset": -1, "symbol": "die", "type": "rel32"}],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "relocation #0" in result.stderr
    assert "offset" in result.stderr


def test_load_rejects_relocation_offset_past_section_end(tmp_path: Path) -> None:
    """A relocation whose patch site doesn't fit in the section fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    _write_ccobj(
        object_payload,
        extern=["die"],
        # Section is 4 bytes; rel32 at offset 2 would need bytes 2..5 — past the end.
        relocations=[{"section": "text", "offset": 2, "symbol": "die", "type": "rel32"}],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\x00\x00\x00\x00",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "relocation #0" in result.stderr
    assert "exceeds" in result.stderr


def test_load_rejects_section_align_not_power_of_two(tmp_path: Path) -> None:
    """A section with non-power-of-two `align` fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    with object_payload.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "extern": [],
                "relocations": [],
                "sections": {"text": {"align": 12, "bytes": ""}},
                "symbols": {},
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "align" in result.stderr
    assert "power of two" in result.stderr


def test_load_rejects_section_align_too_large(tmp_path: Path) -> None:
    """A section with `align` above 4096 fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    with object_payload.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "extern": [],
                "relocations": [],
                "sections": {"text": {"align": 8192, "bytes": ""}},
                "symbols": {},
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "align" in result.stderr


def test_load_rejects_section_bss_negative_size(tmp_path: Path) -> None:
    """A .bss section with a negative size fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    with object_payload.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "extern": [],
                "relocations": [],
                "sections": {"bss": {"align": 4, "size": -1}},
                "symbols": {},
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "bss" in result.stderr
    assert "size" in result.stderr


def test_load_rejects_section_bytes_invalid_base64(tmp_path: Path) -> None:
    """A non-BSS section with non-base64 `bytes` fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    with object_payload.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "extern": [],
                "relocations": [],
                "sections": {"text": {"align": 16, "bytes": "not valid base64!!!"}},
                "symbols": {},
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "text" in result.stderr
    assert "base64" in result.stderr.lower()


def test_load_rejects_section_text_missing_bytes(tmp_path: Path) -> None:
    """A non-BSS section without a `bytes` field fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    with object_payload.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "extern": [],
                "relocations": [],
                "sections": {"text": {"align": 16}},
                "symbols": {},
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "text" in result.stderr
    assert "bytes" in result.stderr


def test_load_rejects_source_not_string(tmp_path: Path) -> None:
    """A `source` field present but not a string is rejected at load."""
    object_payload = tmp_path / "bad.ccobj"
    with object_payload.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "extern": [],
                "relocations": [],
                "sections": {},
                "source": 42,
                "symbols": {},
                "version": 1,
            },
            file,
        )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "source" in result.stderr


def test_load_rejects_symbol_empty_name(tmp_path: Path) -> None:
    """A symbol with an empty-string name is rejected at load."""
    object_payload = tmp_path / "bad.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "non-empty" in result.stderr


def test_load_rejects_symbol_negative_offset(tmp_path: Path) -> None:
    """A symbol with a negative offset fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "text", "offset": -1, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "'main'" in result.stderr
    assert "offset" in result.stderr


def test_load_rejects_symbol_offset_past_section_end(tmp_path: Path) -> None:
    """A symbol whose offset is past the section end fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    _write_ccobj(
        object_payload,
        # text is 1 byte; offset 2 is past the end.
        symbols={"main": {"section": "text", "offset": 2, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "'main'" in result.stderr
    assert "exceeds" in result.stderr


def test_load_rejects_symbol_unknown_section(tmp_path: Path) -> None:
    """A symbol pointing at an unknown section fails at load."""
    object_payload = tmp_path / "bad.ccobj"
    _write_ccobj(
        object_payload,
        symbols={"main": {"section": "ctors", "offset": 0, "binding": "global"}},
        text_bytes=b"\xc3",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "'main'" in result.stderr
    assert "section" in result.stderr


def test_load_rejects_unknown_relocation_type(tmp_path: Path) -> None:
    """A relocation with an unsupported `type` fails at load with the bad value."""
    object_payload = tmp_path / "bad.ccobj"
    _write_ccobj(
        object_payload,
        extern=["foo"],
        relocations=[{"section": "text", "offset": 0, "symbol": "foo", "type": "rel16"}],
        symbols={"main": {"section": "text", "offset": 0, "binding": "global"}},
        text_bytes=b"\x00\x00\x00\x00",
    )
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(tmp_path / "out.bin"), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "'rel16'" in result.stderr


def test_load_rejects_unknown_version(tmp_path: Path) -> None:
    """An unknown version field is a hard error with a clear message."""
    object_payload = tmp_path / "wrong_version.ccobj"
    with object_payload.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "extern": [],
                "relocations": [],
                "sections": {},
                "symbols": {},
                "version": 99,
            },
            file,
        )
    output = tmp_path / "out.bin"
    result = subprocess.run(
        [sys.executable, str(CCLD), "--output", str(output), str(object_payload)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode != 0
    assert "version" in result.stderr.lower()
    assert "99" in result.stderr
