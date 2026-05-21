#!/usr/bin/env python3
"""Tests for cc/ccobj.py and cc.py pack-ccobj / --object modes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "data" / "ccobj"


def test_extern_function_declaration_accepted(tmp_path: Path) -> None:
    """`extern void foo(args);` is accepted as a function declaration.

    In both flat and object modes.
    """
    src = tmp_path / "with_extern.c"
    src.write_text("extern void die(const char *message);\nint main() { return 0; }\n")
    asm_flat = tmp_path / "flat.asm"
    asm_object = tmp_path / "object.asm"

    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", str(src), str(asm_flat)],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm_object)],
        check=True,
    )


def test_flat_mode_extern_call_keeps_bare_call(tmp_path: Path) -> None:
    """Flat mode keeps ``call <name>`` for extern-declared functions.

    Regression guard: in flat mode, the call site keeps the existing
    ``call die`` shape.  Whether NASM ultimately resolves the call is out
    of scope — flat-mode extern calls aren't a supported workflow.
    """
    src = tmp_path / "calls_die.c"
    src.write_text('extern void die(const char *message);\nint main() {\n    die("oops");\n    return 1;\n}\n')
    asm = tmp_path / "calls_die.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert "CCREL_CALL" not in body


def test_flat_mode_unchanged_by_object_plumbing(tmp_path: Path) -> None:
    """Flat mode is unchanged by object-mode plumbing.

    The default (flat) mode keeps emitting ``org`` + ``_program_end`` +
    ``_bss_end`` after the ``--object`` plumbing lands.  Regression guard.
    """
    src = tmp_path / "hello.c"
    src.write_text("int main() { return 0; }\n")
    asm = tmp_path / "hello.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert "org 08048000h" in body
    assert "_program_end:" in body
    assert "_bss_end equ" in body
    assert '%include "ccobj_markers.inc"' not in body


def test_flat_mode_libbboeos_calls_stay_direct(tmp_path: Path) -> None:
    """Flat-mode user code keeps the direct ``jmp FUNCTION_*`` form.

    Regression guard: the indirect-via-pointer-table emission added for
    object-mode linking must not leak into the default flat-binary
    build.  Programs in flat mode keep the 5-byte ``E9 <rel32>`` /
    ``E8 <rel32>`` encodings the kernel's program loader has always
    seen.
    """
    src = tmp_path / "putc.c"
    src.write_text("int main() { putchar('A'); return 0; }\n")
    asm = tmp_path / "putc.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert "call FUNCTION_PRINT_CHARACTER\n" in body, body
    assert "jmp FUNCTION_EXIT\n" in body, body
    assert "[FUNCTION_EXIT_PTR]" not in body
    assert "[FUNCTION_PRINT_CHARACTER_PTR]" not in body


def test_object_mode_emits_data_rodata_bss_sections(tmp_path: Path) -> None:
    """Strings/init globals/zero-init globals land in dedicated sections.

    Object-mode user code lays data out as:
      - initialized globals + initialized arrays    → ``section .data``
      - string literals + local array literals      → ``section .rodata``
      - zero-init globals + elide-frame local cells → ``section .bss``
                                                       (``resb`` reservations)

    Flat-mode output is unchanged — everything still rides inline at
    the tail of ``.text`` under ``org 08048000h``.  This separation
    lets the linker place each kind of storage independently when
    composing multiple translation units.
    """
    src = tmp_path / "mixed.c"
    src.write_text(
        "int g_init = 42;\n"
        "int g_array[3] = {1, 2, 3};\n"
        "int g_zero;\n"
        "int main() {\n"
        "    g_zero = g_init;\n"
        '    printf("%d\\n", g_zero);\n'
        "    return 0;\n"
        "}\n"
    )
    asm = tmp_path / "mixed.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    # Initialized globals → .data
    assert "section .data" in body
    assert "_g_g_init: dd 42" in body
    assert "_g_g_array: dd 1, 2, 3" in body
    # Strings → .rodata
    assert "section .rodata" in body
    assert "_str_0:" in body
    # Zero-init globals → .bss via resb (no BSS trailer / no EQUs)
    assert "section .bss" in body
    assert "_g_g_zero: resb 4" in body
    assert "dw 0B032h" not in body
    assert "_g_g_zero equ" not in body
    # Data labels must not appear inside the .text body.
    text_section, _, _rest = body.partition("section .data")
    assert "_g_g_init:" not in text_section
    assert "_g_g_array:" not in text_section
    assert "_str_0:" not in text_section


def test_object_mode_emits_elided_locals_in_bss(tmp_path: Path) -> None:
    """``main``'s static-storage locals land in ``section .bss``.

    ``main`` is the only function that's elide-frame'd by default, so
    its locals are promoted to per-program static storage at
    ``_l_<name>``.  Flat mode emits the zero cells inline at the tail
    of the function body; object mode pushes them into ``section .bss``
    as ``resb`` reservations so the .text section stays code-only.
    """
    src = tmp_path / "main_local.c"
    # ``&byte`` forces the local out of a register into static storage
    # under elide_frame.  Without an address-of, cc.py keeps it in a
    # register and no ``_l_`` cell is emitted.
    src.write_text("int main() {\n    char byte;\n    read(STDIN, &byte, 1);\n    return 0;\n}\n")
    asm = tmp_path / "main_local.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert "section .bss" in body
    assert "_l_byte: resb 1" in body
    # No inline ``_l_byte: db 0`` cell in .text.
    text_section, _, _rest = body.partition("section .bss")
    assert "_l_byte:" not in text_section


def test_object_mode_emits_section_directives(tmp_path: Path) -> None:
    """Object mode emits section directives and global declarations.

    Checks that ``--object`` emits ``section .text`` (not ``org``),
    ``global main``, ``%include "ccobj_markers.inc"``, and suppresses the
    flat-mode ``_program_end``/``_bss_end`` trailer.
    """
    src = tmp_path / "hello.c"
    src.write_text("int main() { return 0; }\n")
    asm = tmp_path / "hello.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert '%include "ccobj_markers.inc"' in body
    assert "section .text" in body
    assert "global main" in body
    assert "org 08048000h" not in body
    assert "_program_end:" not in body
    assert "_bss_end equ" not in body


def test_object_mode_extern_call_uses_ccrel_macro(tmp_path: Path) -> None:
    """Calls to extern-declared functions emit ``CCREL_CALL`` in object mode.

    In --object mode, calls to functions declared but not defined in
    the translation unit are emitted as ``CCREL_CALL <name>`` instead of
    ``call <name>``.
    """
    src = tmp_path / "calls_die.c"
    src.write_text('extern void die(const char *message);\nint main() {\n    die("oops");\n    return 1;\n}\n')
    asm = tmp_path / "calls_die.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert "CCREL_CALL die" in body, f"expected CCREL_CALL die in:\n{body}"
    # No bare `call die` anywhere outside the CCREL_CALL macro.
    body_minus_ccrel = body.replace("CCREL_CALL die", "")
    assert "call die" not in body_minus_ccrel


def test_object_mode_libbboeos_calls_use_indirect_form(tmp_path: Path) -> None:
    """Libbboeos calls/jumps emit the indirect ``call/jmp [FUNCTION_*_PTR]`` form.

    Object-mode binaries are placed at PROGRAM_BASE by ``ccld``, which
    means any PC-relative jump baked in at assembly time is wrong by
    the program's base address.  Switching libbboeos sites to the indirect-
    through-FUNCTION_POINTER_TABLE form (``FF 15``/``FF 25 <abs32>``)
    makes the call site base-invariant: NASM emits the abs32 verbatim,
    pack-ccobj copies the bytes through, and ccld doesn't need to
    relocate them.
    """
    src = tmp_path / "putc.c"
    src.write_text("int main() { putchar('A'); return 0; }\n")
    asm = tmp_path / "putc.asm"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm)],
        check=True,
    )

    body = asm.read_text()
    assert "call [FUNCTION_PRINT_CHARACTER_PTR]" in body, body
    assert "jmp [FUNCTION_EXIT_PTR]" in body, body
    # Direct-form residue would be a regression.
    assert "call FUNCTION_PRINT_CHARACTER\n" not in body
    assert "jmp FUNCTION_EXIT\n" not in body


def test_pack_ccobj_auto_relocates_cross_section_abs32(tmp_path: Path) -> None:
    """Cross-section abs32 references in NASM listings become relocations.

    cc.py --object emits in-translation-unit data references as bare
    instructions (``mov eax, [_g_x]``, ``push _str_0``), not CCREL_*
    macros, because the target symbol lives in a different section of
    the same object file.  NASM emits these with bracketed placeholder
    bytes (``A1[00000000]``).  pack-ccobj must recognise the bracket
    form, accumulate the opcode + 4-byte placeholder into the section,
    and emit an abs32 relocation pointing at the symbol named on the
    source line — so the linker can patch the placeholder with the
    final absolute address when the object lands in the program image.
    """
    src = tmp_path / "globals.c"
    src.write_text('int g_init = 42;\nint g_zero;\nint main() {\n    g_zero = g_init;\n    printf("%d\\n", g_zero);\n    return 0;\n}\n')
    asm = tmp_path / "globals.asm"
    binary = tmp_path / "globals.bin"
    listing = tmp_path / "globals.lst"
    obj = tmp_path / "globals.ccobj"
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm)],
        check=True,
    )
    subprocess.run(
        [
            "nasm",
            "-f",
            "bin",
            "-i",
            str(REPO_ROOT / "kernel" / "include") + "/",
            "-l",
            str(listing),
            "-o",
            str(binary),
            str(asm),
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(CC), "pack-ccobj", str(binary), str(listing), str(obj)],
        check=True,
    )

    with obj.open(encoding="utf-8") as file:
        payload = json.load(file)

    # The cross-section references should produce abs32 relocations
    # for every in-TU symbol the .text body touches.
    reloc_symbols = {reloc["symbol"] for reloc in payload["relocations"]}
    assert reloc_symbols >= {"_g_g_init", "_g_g_zero", "_str_0"}, reloc_symbols
    for reloc in payload["relocations"]:
        if reloc["symbol"] in {"_g_g_init", "_g_g_zero", "_str_0"}:
            assert reloc["type"] == "abs32", reloc
            assert reloc["section"] == "text", reloc

    # Every relocation target must be a defined symbol in this object.
    defined_symbols = set(payload["symbols"])
    assert reloc_symbols <= defined_symbols, reloc_symbols - defined_symbols
    # ``_g_g_zero`` is BSS-only, so it lands in the ``.bss`` section.
    assert payload["symbols"]["_g_g_zero"]["section"] == "bss"


def test_pack_ccobj_basic_fixture(tmp_path: Path) -> None:
    """Fixture .asm exercises every CCREL_* macro.

    The produced .ccobj must list every global symbol and every relocation.
    """
    output = tmp_path / "fixture_basic.ccobj"
    subprocess.run(
        [
            sys.executable,
            str(CC),
            "pack-ccobj",
            str(FIXTURE_DIR / "fixture_basic.bin"),
            str(FIXTURE_DIR / "fixture_basic.lst"),
            str(output),
        ],
        check=True,
    )

    with output.open() as file:
        data = json.load(file)

    assert data["version"] == 1
    assert set(data["symbols"]) == {"main", "helper", "format_string", "counter", "scratch"}
    assert data["symbols"]["main"] == {"section": "text", "offset": 0, "binding": "global"}
    assert data["symbols"]["helper"]["section"] == "text"
    assert data["symbols"]["format_string"]["section"] == "rodata"
    assert data["symbols"]["counter"]["section"] == "data"
    assert data["symbols"]["scratch"]["section"] == "bss"

    assert set(data["extern"]) == {"die", "_exit", "errno"}

    reloc_summary = [(reloc["symbol"], reloc["type"], reloc["section"]) for reloc in data["relocations"]]
    assert ("die", "rel32", "text") in reloc_summary
    assert ("_exit", "rel32", "text") in reloc_summary
    assert reloc_summary.count(("errno", "abs32", "text")) == 2

    assert data["sections"]["text"]["align"] >= 4
    assert data["sections"]["bss"]["size"] == 64  # 16 dwords


def test_round_trip_c_to_ccobj(tmp_path: Path) -> None:
    """End-to-end: C source --object → NASM .bin/.lst → pack-ccobj.

    Produces a .ccobj that lists `main` as global and `die` as an
    extern with a rel32 relocation.
    """
    src = tmp_path / "calls_die.c"
    src.write_text('extern void die(const char *message);\nint main() {\n    die("oops");\n    return 1;\n}\n')
    asm = tmp_path / "calls_die.asm"
    bin_path = tmp_path / "calls_die.bin"
    lst = tmp_path / "calls_die.lst"
    ccobj = tmp_path / "calls_die.ccobj"

    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", str(src), str(asm)],
        check=True,
    )
    subprocess.run(
        [
            "nasm",
            "-f",
            "bin",
            "-i",
            str(REPO_ROOT / "kernel" / "include") + "/",
            "-l",
            str(lst),
            "-o",
            str(bin_path),
            str(asm),
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(CC),
            "pack-ccobj",
            str(bin_path),
            str(lst),
            str(ccobj),
        ],
        check=True,
    )

    with ccobj.open() as file:
        data = json.load(file)
    assert "main" in data["symbols"]
    assert data["symbols"]["main"]["binding"] == "global"
    assert data["symbols"]["main"]["section"] == "text"
    assert "die" in data["extern"]
    reloc_symbols = [reloc["symbol"] for reloc in data["relocations"]]
    assert reloc_symbols.count("die") == 1
    die_reloc = next(r for r in data["relocations"] if r["symbol"] == "die")
    assert die_reloc["type"] == "rel32"
    assert die_reloc["section"] == "text"


if __name__ == "__main__":
    sys.exit(0 if test_pack_ccobj_basic_fixture(Path("/tmp/test_ccobj_run")) is None else 1)
