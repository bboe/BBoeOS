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


def test_flat_mode_vdso_calls_stay_direct(tmp_path: Path) -> None:
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


def test_object_mode_vdso_calls_use_indirect_form(tmp_path: Path) -> None:
    """VDSO calls/jumps emit the indirect ``call/jmp [FUNCTION_*_PTR]`` form.

    Object-mode binaries are placed at PROGRAM_BASE by ``ccld``, which
    means any PC-relative jump baked in at assembly time is wrong by
    the program's base address.  Switching vDSO sites to the indirect-
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
            str(REPO_ROOT / "src" / "include") + "/",
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
