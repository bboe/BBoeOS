"""Pytest tests for cc.py struct support (PR 0).

Verifies:
  - Packed struct layout and sizeof
  - ptr->field read/write codegen with correct byte offsets
  - Global struct array emits correct BSS size expression
  - struct fd layout matches FD_OFFSET_* constants from constants.asm exactly
  - Existing programs still compile and assemble under both --bits 16 and 32
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CC = REPO_ROOT / "cc.py"
INCLUDE_DIR = REPO_ROOT / "src" / "include"

# FD layout constants from src/include/constants.asm (must match exactly).
FD_OFFSET_TYPE = 0
FD_OFFSET_FLAGS = 1
FD_OFFSET_START = 2
FD_OFFSET_SIZE = 4
FD_OFFSET_POSITION = 8
FD_OFFSET_DIRECTORY_SECTOR = 12
FD_OFFSET_DIRECTORY_OFFSET = 14
FD_OFFSET_MODE = 16
FD_ENTRY_SIZE = 32


def _compile(source_text: str, bits: int = 16) -> str:
    """Compile *source_text* with cc.py and return the generated assembly."""
    text = textwrap.dedent(source_text)
    with tempfile.TemporaryDirectory(prefix="test_struct_") as work:
        work_path = Path(work)
        src = work_path / "test.c"
        out = work_path / "test.asm"
        src.write_text(text)
        result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), str(src), str(out)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"cc.py failed:\n{result.stderr}")
        return out.read_text()


def _compile_and_assemble(source_text: str, bits: int = 16) -> None:
    """Compile *source_text* and assemble with nasm, failing on any error."""
    text = textwrap.dedent(source_text)
    with tempfile.TemporaryDirectory(prefix="test_struct_") as work:
        work_path = Path(work)
        src = work_path / "test.c"
        asm = work_path / "test.asm"
        binary = work_path / "test.bin"
        src.write_text(text)
        cc_result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), str(src), str(asm)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if cc_result.returncode != 0:
            pytest.fail(f"cc.py failed:\n{cc_result.stderr}")
        nasm_result = subprocess.run(
            ["nasm", "-f", "bin", "-i", str(INCLUDE_DIR) + "/", str(asm), "-o", str(binary)],
            capture_output=True,
            check=False,
            text=True,
        )
        if nasm_result.returncode != 0:
            pytest.fail(f"nasm failed:\n{nasm_result.stderr}\n--- asm ---\n{asm.read_text()}")


# --- sizeof tests ---


def test_sizeof_packed_char_int_16bit() -> None:
    """sizeof(struct {char a; int b;}) == 3 in --bits 16 (packed, no padding)."""
    asm = _compile(
        """
        struct pair { char a; int b; };
        int get_size() {
            return sizeof(struct pair);
        }
        int main() { return 0; }
    """,
        bits=16,
    )
    assert "mov ax, 3" in asm, f"Expected 'mov ax, 3' for sizeof packed {{char+int}}\n{asm}"


def test_sizeof_packed_char_int_32bit() -> None:
    """sizeof(struct {char a; int b;}) == 5 in --bits 32 (char=1, int=4)."""
    asm = _compile(
        """
        struct pair { char a; int b; };
        int get_size() {
            return sizeof(struct pair);
        }
        int main() { return 0; }
    """,
        bits=32,
    )
    assert "mov eax, 5" in asm, f"Expected 'mov eax, 5' for sizeof packed {{char+int}} (32-bit)\n{asm}"


def test_sizeof_fd_struct_16bit() -> None:
    """sizeof(struct fd) == FD_ENTRY_SIZE (32) in --bits 16."""
    asm = _compile(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        int get_size() {
            return sizeof(struct fd);
        }
        int main() { return 0; }
    """,
        bits=16,
    )
    assert f"mov ax, {FD_ENTRY_SIZE}" in asm, f"Expected 'mov ax, {FD_ENTRY_SIZE}' for sizeof(struct fd)\n{asm}"


# --- member access offset tests ---


def test_member_access_offset_zero() -> None:
    """p->type (offset 0) emits [bx] with no +offset."""
    asm = _compile(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        void set_type(struct fd *p) {
            p->type = 1;
        }
    """,
        bits=16,
    )
    # byte store at offset 0: mov byte [bx], al
    assert "[bx]" in asm and "bx+" not in asm.split("set_type")[1].split("ret")[0], (
        f"Expected '[bx]' (no +offset) for field at offset 0\n{asm}"
    )


def test_member_access_offset_flags() -> None:
    """p->flags (offset 1) emits [bx+1]."""
    asm = _compile(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        void set_flags(struct fd *p) {
            p->flags = 2;
        }
    """,
        bits=16,
    )
    assert f"[bx+{FD_OFFSET_FLAGS}]" in asm, f"Expected '[bx+{FD_OFFSET_FLAGS}]' for flags field\n{asm}"


def test_member_access_offset_start() -> None:
    """p->start (offset 2) emits [bx+2]."""
    asm = _compile(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        void set_start(struct fd *p) {
            p->start = 3;
        }
    """,
        bits=16,
    )
    assert f"[bx+{FD_OFFSET_START}]" in asm, f"Expected '[bx+{FD_OFFSET_START}]' for start field\n{asm}"


def test_member_read_and_write_roundtrip() -> None:
    """p->flags = x; y = p->flags; compiles and assembles cleanly."""
    _compile_and_assemble(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        int roundtrip(struct fd *p, int x) {
            p->flags = x;
            int y;
            y = p->flags;
            return y;
        }
    """,
        bits=16,
    )


def test_member_access_in_condition() -> None:
    """p->type can be compared in an if condition."""
    _compile_and_assemble(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        int is_free(struct fd *p) {
            if (p->type == 0) {
                return 1;
            }
            return 0;
        }
    """,
        bits=16,
    )


def test_member_access_uint32_read_32bit() -> None:
    """p->size where size is uint32_t emits a full 4-byte load in 32-bit mode."""
    asm = _compile(
        """
        struct fd {
            uint8_t type;
            uint8_t flags;
            uint16_t start;
            uint32_t size;
        };
        int read_size(struct fd *p) {
            return p->size;
        }
    """,
        bits=32,
    )
    assert "mov eax, [ebx+4]" in asm, f"Expected 'mov eax, [ebx+4]' for uint32_t field read\n{asm}"


def test_member_access_uint32_write_32bit() -> None:
    """p->size = x where size is uint32_t emits a full 4-byte store in 32-bit mode."""
    asm = _compile(
        """
        struct fd {
            uint8_t type;
            uint8_t flags;
            uint16_t start;
            uint32_t size;
        };
        void write_size(struct fd *p, int value) {
            p->size = value;
        }
    """,
        bits=32,
    )
    assert "mov [ebx+4], eax" in asm, f"Expected 'mov [ebx+4], eax' for uint32_t field write\n{asm}"


def test_member_access_uint16_read_32bit() -> None:
    """p->start where start is uint16_t emits ``movzx eax, word [...]`` in 32-bit mode.

    Without the zero-extend, the load would either spill into adjacent
    bytes (32-bit ``mov eax, [...]``) or leave EAX's upper word stale
    from a prior write — ``test eax, eax`` checks downstream would
    misfire.
    """
    asm = _compile(
        """
        struct fd {
            uint8_t type;
            uint8_t flags;
            uint16_t start;
        };
        int read_start(struct fd *p) {
            return p->start;
        }
    """,
        bits=32,
    )
    assert "movzx eax, word [ebx+2]" in asm, f"Expected 'movzx eax, word [ebx+2]' for uint16_t field read\n{asm}"


def test_member_access_uint16_write_32bit() -> None:
    """p->start = x emits ``mov word [...], ax`` in 32-bit mode.

    The destination needs an explicit ``word`` size override; the
    default ``mov [...], eax`` would clobber the next 2 bytes of the
    struct.
    """
    asm = _compile(
        """
        struct fd {
            uint8_t type;
            uint8_t flags;
            uint16_t start;
        };
        void write_start(struct fd *p, int value) {
            p->start = value;
        }
    """,
        bits=32,
    )
    assert "mov word [ebx+2], ax" in asm, f"Expected 'mov word [ebx+2], ax' for uint16_t field write\n{asm}"


# --- global struct array tests ---


def test_global_struct_array_bss_size() -> None:
    """Global struct array sizes to (N * sizeof(struct)) in the BSS trailer.

    cc.py reserves BSS via the trailer-magic protocol — emit ``dd N``
    + ``dw 0xB032`` and let program_enter zero-fill at load — rather
    than allocating bytes in the binary.  For ``struct item table[5]``
    where struct item is 3 bytes (char=1 + int=2), the trailer must
    declare 15 bytes via ``_bss_end equ _program_end + 15``.
    """
    asm = _compile(
        """
        struct item { char x; int y; };
        struct item table[5];
        int main() {
            return 0;
        }
    """,
        bits=16,
    )
    assert "_bss_end equ _program_end + 15" in asm, f"Expected '_bss_end equ _program_end + 15' for 5-element struct array BSS\n{asm}"


def test_global_struct_array_compiles_and_assembles() -> None:
    """Global struct fd array with symbolic size compiles and assembles."""
    _compile_and_assemble(
        """
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        struct fd fd_table[8];
        int main() {
            return 0;
        }
    """,
        bits=16,
    )


# --- FD layout pinning test ---


def test_fd_layout_all_offsets() -> None:
    """Verify each field of struct fd is accessed at the exact FD_OFFSET_* byte offset.

    This is the canonical correctness gate for the fd.c port: if any field
    drifts from its FD_OFFSET_* constant, the C code and the asm callers
    will disagree on struct layout and silently corrupt the FD table.
    """
    source = textwrap.dedent("""
        struct fd {
            char type;
            char flags;
            int start;
            unsigned long size;
            unsigned long position;
            int directory_sector;
            int directory_offset;
            char mode;
            char _reserved[15];
        };
        void write_type(struct fd *p) { p->type = 0; }
        void write_flags(struct fd *p) { p->flags = 0; }
        void write_start(struct fd *p) { p->start = 0; }
        void write_directory_sector(struct fd *p) { p->directory_sector = 0; }
        void write_directory_offset(struct fd *p) { p->directory_offset = 0; }
        void write_mode(struct fd *p) { p->mode = 0; }
        int main() { return 0; }
    """)
    asm = _compile(source, bits=16)

    def _section(asm_text: str, function_name: str) -> str:
        start = asm_text.find(f"{function_name}:")
        if start == -1:
            return ""
        end = asm_text.find("\nret", start)
        return asm_text[start : end + 4] if end != -1 else asm_text[start:]

    assert "[bx]" in _section(asm, "write_type"), f"type (offset 0) should use [bx]\n{_section(asm, 'write_type')}"
    assert f"[bx+{FD_OFFSET_FLAGS}]" in _section(asm, "write_flags"), (
        f"flags should be at offset {FD_OFFSET_FLAGS}\n{_section(asm, 'write_flags')}"
    )
    assert f"[bx+{FD_OFFSET_START}]" in _section(asm, "write_start"), (
        f"start should be at offset {FD_OFFSET_START}\n{_section(asm, 'write_start')}"
    )
    assert f"[bx+{FD_OFFSET_DIRECTORY_SECTOR}]" in _section(asm, "write_directory_sector"), (
        f"directory_sector should be at offset {FD_OFFSET_DIRECTORY_SECTOR}\n{_section(asm, 'write_directory_sector')}"
    )
    assert f"[bx+{FD_OFFSET_DIRECTORY_OFFSET}]" in _section(asm, "write_directory_offset"), (
        f"directory_offset should be at offset {FD_OFFSET_DIRECTORY_OFFSET}\n{_section(asm, 'write_directory_offset')}"
    )
    assert f"[bx+{FD_OFFSET_MODE}]" in _section(asm, "write_mode"), (
        f"mode should be at offset {FD_OFFSET_MODE}\n{_section(asm, 'write_mode')}"
    )


# --- regression: existing user programs still compile and assemble ---


@pytest.mark.parametrize("source_path", sorted((REPO_ROOT / "src" / "c").glob("*.c")))
@pytest.mark.parametrize("bits", [16, 32])
def test_existing_programs_unchanged(source_path: Path, bits: int) -> None:
    """Every existing user-space C program still compiles and assembles after PR 0."""
    with tempfile.TemporaryDirectory(prefix="test_struct_regression_") as work:
        work_path = Path(work)
        asm = work_path / f"{source_path.stem}.asm"
        binary = work_path / f"{source_path.stem}.bin"

        cc_result = subprocess.run(
            ["python3", str(CC), "--bits", str(bits), str(source_path), str(asm)],
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            text=True,
        )
        if cc_result.returncode != 0:
            pytest.fail(f"cc.py failed for {source_path.name} --bits {bits}:\n{cc_result.stderr}")

        nasm_result = subprocess.run(
            ["nasm", "-f", "bin", "-i", str(INCLUDE_DIR) + "/", str(asm), "-o", str(binary)],
            capture_output=True,
            check=False,
            text=True,
        )
        if nasm_result.returncode != 0:
            pytest.fail(f"nasm failed for {source_path.name} --bits {bits}:\n{nasm_result.stderr}")
