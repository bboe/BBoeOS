#!/usr/bin/env python3
"""Runtime smoke tests for programs loaded from an ext2 filesystem.

Builds the OS with `make_os.sh --ext2`, boots in QEMU, runs a representative
command for each test program, and checks the output against an expected regex.
Each test gets its own copy of the base image so writes don't affect other
tests.  After QEMU exits, ``e2fsck -f -n`` runs on the modified image to check
filesystem integrity.

Programs that read file content via `io_read` (e.g. `cat`) exercise the
`vfs_read_sec` function pointer, which routes through `ext2_read_sec` to
translate byte positions to ext2 block lookups.  Programs that list directory
contents via `fd_read_dir` (e.g. `ls`) exercise the `vfs_read_dir_fn` function
pointer, which routes through `ext2_read_dir` to translate ext2 variable-length
directory entries into the fixed 32-byte bbfs format.

Usage:
    ./test_ext2.py            # run the full suite
    ./test_ext2.py hello      # run one program
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_IMAGE = "drive_ext2.img"

sys.path.insert(0, str(REPO_ROOT))

from run_qemu import run_commands  # noqa: E402

from add_file import add_empty_files, add_file, compute_directory_sector, ext2_add_file  # noqa: E402

_DEFAULT_PROGRAM_TIMEOUT = float(os.environ.get("BBOE_PROGRAM_TIMEOUT", "1.0"))
_LARGE_FILE_TIMEOUT = float(os.environ.get("BBOE_LARGE_FILE_TIMEOUT", "6.0"))
_DOUBLY_INDIRECT_TIMEOUT = float(os.environ.get("BBOE_DOUBLY_INDIRECT_TIMEOUT", "12.0"))


@dataclass
class ProgramTest:
    """One runtime test: shell commands to run and a regex the output must match.

    A ``setup`` hook receives ``(image, test)`` after the per-test image
    is copied but before QEMU boots.  It may mutate ``test.commands`` and
    ``test.expect`` — used by ``exec_first_middle_last`` to pick filler
    names based on the post-setup directory layout instead of hard-coding
    them.
    """

    name: str
    commands: list[str]
    expect: str
    setup: Callable[[Path, ProgramTest], None] | None = None
    slow: bool = False
    timeout: float = _DEFAULT_PROGRAM_TIMEOUT


TESTS: list[ProgramTest] = [
    ProgramTest("cat", ["cat src/parse_ip.asm"], r"^parse_ip:"),
    ProgramTest("cat_large", ["cat src/asm.c"], r"Self-hosted x86 assembler", slow=True, timeout=_LARGE_FILE_TIMEOUT),
    ProgramTest(
        "chmod",
        ["cp src/parse_ip.asm out.asm", "chmod +x out.asm", "ls"],
        r"out\.asm\*",
    ),
    ProgramTest("cp", ["cp src/parse_ip.asm out.asm", "cat out.asm"], r"^parse_ip:"),
    ProgramTest(
        "cp_into_subdir",
        ["mkdir mydir", "cp src/parse_ip.asm mydir/copy.asm", "cat mydir/copy.asm"],
        r"^parse_ip:",
    ),
    ProgramTest(
        "cp_overwrite_shrink",
        ["cp src/asm.c out.c", "cp src/parse_ip.asm out.c", "cat out.c"],
        r"^parse_ip:",
        timeout=_LARGE_FILE_TIMEOUT,
    ),
    ProgramTest(
        "doubly_indirect_cat",
        ["cat src/large.bin"],
        r"EXT2_DOUBLY_INDIRECT_OK",  # sentinel placed at byte 274432 (block 268)
        slow=True,
        timeout=_DOUBLY_INDIRECT_TIMEOUT,
    ),
    ProgramTest(
        "doubly_indirect_cp",
        ["cp src/large.bin out.bin", "cat out.bin"],
        r"EXT2_DOUBLY_INDIRECT_OK",  # verifies doubly-indirect write path
        slow=True,
        timeout=_DOUBLY_INDIRECT_TIMEOUT,
    ),
    ProgramTest(
        "doubly_indirect_cp_shrink",
        ["cp src/large.bin out.bin", "cp src/parse_ip.asm out.bin", "cat out.bin"],
        r"^parse_ip:",
        timeout=_DOUBLY_INDIRECT_TIMEOUT,
    ),
    ProgramTest("echo", ["echo ext2"], r"^ext2$"),
    ProgramTest(
        # Pad bin/ with empty fillers until its inode uses all 12 direct
        # blocks (ext2_search_dir's walk ceiling), interleaving three
        # executable probes — _zexec_a (block 1 first entry), _zexec_b
        # (~middle of the directory), _zexec_last (literal final entry).
        # Looking up any one of them forces ext2_search_blk to advance
        # past the 512-byte sector boundary in at least one preceding
        # block, which is the path the previous commit fixes.  The
        # setup writes the actual probe names into commands+expect so
        # the assertions stay correct regardless of how many entries
        # the rest of bin/ accumulates.
        "exec_first_middle_last",
        commands=[],
        expect="",
        setup=lambda image, test: _pad_bin_to_full_directory(image=image, test=test),
    ),
    ProgramTest("hello", ["hello"], r"Hello world!"),
    ProgramTest("ls", ["ls bin"], r"hello\*"),
    ProgramTest(
        "mkdir",
        ["mkdir mydir", "ls mydir"],
        r"^\.\./",  # '..' entry always present
    ),
    ProgramTest(
        "mkdir_ls_root",
        ["mkdir mydir", "ls"],
        r"mydir/",
    ),
    ProgramTest(
        "mkdir_nested",
        ["mkdir parent", "mkdir parent/child", "ls parent/child"],
        r"^\.\./",
    ),
    ProgramTest(
        # `_add_multi_sector_dir_filler` (run as a per-test setup) keeps
        # appending _zzpadNN stubs to bin/ until one lands in the *last*
        # 512-byte sector of bin/'s first directory block — byte ≥ 512
        # on 1 KB blocks, byte ≥ 1536 on 2 KB blocks — then writes the
        # name of that probe into commands+expect.  Confirms ext2_search_blk
        # advances across every intra-block sector boundary (0→1 on 1 KB;
        # 0→1→2→3 on 2 KB).
        "multi_sector_dir",
        commands=[],
        expect="",
        setup=lambda image, test: _add_multi_sector_dir_filler(image=image, test=test),
    ),
    ProgramTest(
        "rename",
        ["cp src/parse_ip.asm out.asm", "mv out.asm renamed.asm", "cat renamed.asm"],
        r"^parse_ip:",
    ),
    ProgramTest(
        "rename_cross_parent",
        ["mkdir sub", "cp src/parse_ip.asm sub/file.asm", "mv sub/file.asm out.asm", "cat out.asm"],
        r"^parse_ip:",
    ),
    ProgramTest(
        "rename_dir",
        ["mkdir mydir", "mv mydir newdir", "ls newdir"],
        r"^\.\./",
    ),
    ProgramTest(
        "rename_dir_cross_parent",
        ["mkdir sub", "mkdir mydir", "mv mydir sub/mydir", "ls sub/mydir"],
        r"^\.\./",
    ),
    ProgramTest(
        "rm",
        ["cp src/parse_ip.asm out.asm", "rm out.asm", "cat out.asm"],
        r"File not found",
    ),
    ProgramTest(
        "rmdir",
        ["mkdir mydir", "rmdir mydir", "ls mydir"],
        r"Not found",  # ls fails because mydir was successfully removed
    ),
    ProgramTest(
        "rmdir_nonempty",
        ["mkdir mydir", "cp src/parse_ip.asm mydir/file.asm", "rmdir mydir"],
        r"Not empty",
    ),
    ProgramTest("uptime", ["uptime"], r"\d+:\d{2}:\d{2}"),
]


DOUBLY_INDIRECT_SENTINEL = b"EXT2_DOUBLY_INDIRECT_OK"
DOUBLY_INDIRECT_START = (12 + 256) * 1024  # byte 274432 = first doubly-indirect block
EXT2_DIRECT_BLOCKS = 12  # ext2 directory blocks ext2_search_dir walks (i_block[0..11])


def _add_exec_probe(*, image: Path, name: str) -> None:
    """Compile a tiny C program that prints `EXEC <name>` and add it to bin/."""
    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / f"{name}.c"
        source.write_text(f'int main() {{ printf("EXEC {name}\\n"); return 0; }}\n')
        assembled = Path(tmpdir) / f"{name}.asm"
        subprocess.run(
            ["./cc.py", "--bits", "32", str(source), str(assembled)],
            check=True,
            cwd=str(REPO_ROOT),
        )
        binary = Path(tmpdir) / name
        subprocess.run(
            ["nasm", "-f", "bin", "-i", "src/include/", "-o", str(binary), str(assembled)],
            check=True,
            cwd=str(REPO_ROOT),
        )
        add_file(
            executable=True,
            file_path=str(binary),
            image_path=str(image),
            subdirectory="bin",
        )


def _add_large_test_file(*, image: Path) -> None:
    """Inject a 280 KB file into src/ to exercise the doubly-indirect block paths.

    With 1 KB blocks the doubly-indirect threshold is 268 KB (12 direct + 256
    singly-indirect).  280 KB puts 12 data blocks into the doubly-indirect
    region, covering both the allocation and free paths.

    A sentinel string is written at byte 274432 (start of block 268, the first
    doubly-indirect block) so tests can confirm that reads and writes actually
    reach the doubly-indirect region rather than matching content in the direct
    or singly-indirect range.
    """
    target_bytes = 280 * 1024
    source = (REPO_ROOT / "src" / "c" / "asm.c").read_bytes()
    content = bytearray((source * (target_bytes // len(source) + 1))[:target_bytes])
    content[DOUBLY_INDIRECT_START : DOUBLY_INDIRECT_START + len(DOUBLY_INDIRECT_SENTINEL)] = DOUBLY_INDIRECT_SENTINEL
    with tempfile.TemporaryDirectory() as tmpdir:
        large_file = Path(tmpdir) / "large.bin"
        large_file.write_bytes(content)
        ext2_add_file(
            executable=False,
            ext2_start_sector=compute_directory_sector(image_path=str(image)),
            file_path=str(large_file),
            image_path=str(image),
            subdirectory="src",
        )


def _add_multi_sector_dir_filler(*, image: Path, test: ProgramTest) -> None:
    """Pad bin/ until an entry lands in block 0's final 512-byte sector.

    ext2_search_blk reads one 512-byte sector at a time and walks the
    entries inside it; on a miss it bumps the within-block sector index
    and reads the next sector.  A regression that loses the sector
    counter or the block number across iterations only surfaces when a
    target entry actually lives past byte 512 of its block.  1 KB
    blocks span two sectors (boundary at 512); 2 KB blocks span four
    (boundaries at 512, 1024, 1536), so a fixed handful of stubs
    enough to cross the first boundary on 1 KB blocks does not exercise
    the 1→2 or 2→3 advances on 2 KB blocks.

    Keeps adding _zzpadNN stubs to bin/ until the next entry would land
    at or past `block_size - 512` (i.e. inside the final intra-block
    sector), then captures that stub's name as the assertion target.
    Looking it up forces ext2_search_blk to walk every intra-block
    sector boundary of block 0.
    """
    block_size = _ext2_block_size(image=image)
    last_sector_start = block_size - 512
    initial_offset = _bin_block0_used_bytes(image=image)
    # Each "_zzpadNN" entry is 8 (ext2 dirent header) + ((8+1+3)&~3) = 20 bytes,
    # so the Nth (0-indexed) stub starts at initial_offset + N*stub_size.  Solve
    # for the smallest N whose start offset is at or past last_sector_start —
    # the entry whose lookup forces a walk across every intra-block sector
    # boundary.  When initial_offset is already past the boundary (1 KB blocks
    # land here because the baseline bin/ pads exactly to byte 512), the very
    # first stub serves and target_index is 0.
    stub_size = 20
    target_index = max(0, (last_sector_start - initial_offset + stub_size - 1) // stub_size)
    needed = target_index + 1
    ext2_start = compute_directory_sector(image_path=str(image))
    with tempfile.TemporaryDirectory() as tmpdir:
        stubs = []
        for index in range(needed):
            stub = Path(tmpdir) / f"_zzpad{index:02d}"
            stub.write_text("MULTISEC\n")
            stubs.append(stub)
        # Batch every `write` into a single debugfs session with a single
        # partition extract+splice, instead of paying ext2_add_file's
        # per-call dd round-trip ~65 times on 2 KB blocks.
        partition = Path(tmpdir) / "partition.ext2"
        subprocess.run(
            ["dd", f"if={image}", f"of={partition}", "bs=512", f"skip={ext2_start}", "status=none"],
            check=True,
        )
        script = "".join(f"write {stub} /bin/{stub.name}\n" for stub in stubs)
        result = subprocess.run(
            ["debugfs", "-w", str(partition)],
            input=script.encode(),
            capture_output=True,
            check=False,
        )
        stderr_text = result.stderr.decode()
        stderr_failed = any(
            line.strip() for line in stderr_text.splitlines() if not line.startswith("debugfs ") and "Allocated inode:" not in line
        )
        if result.returncode != 0 or stderr_failed:
            msg = f"debugfs batch write failed:\n{stderr_text}"
            raise RuntimeError(msg)
        subprocess.run(
            ["dd", f"if={partition}", f"of={image}", "bs=512", f"seek={ext2_start}", "conv=notrunc", "status=none"],
            check=True,
        )
    test.commands = [f"cat bin/_zzpad{target_index:02d}"]
    test.expect = r"^MULTISEC$"


def _add_straddle_dir_filler(*, image: Path, test: ProgramTest) -> None:
    """Place an entry whose 8-byte name spans a 512-byte sector boundary.

    Pads bin/ with a chain of filler entries (rec_lens 12 / 16 / 20 via
    name_lens 4 / 8 / 12) so the next entry — STRADDLE, name_len 8 —
    has its header at offset boundary - 8 of bin/'s first block: header
    in the lo 512-byte sector, name in the hi sector.  Looking it up
    forces ext2_search_blk's name compare to read across the 512-byte
    boundary; a regression that uses only the lo half of its sliding
    window compares against stale bytes and reports the entry missing.

    Stronger guarantee than `_add_multi_sector_dir_filler`, which adds
    fixed-size stubs and only happens to straddle for specific
    block_size + bin/ layouts.
    """
    block_size = _ext2_block_size(image=image)
    initial_offset = _bin_block0_used_bytes(image=image)
    target_header_offset = _pick_straddle_target_offset(block_size=block_size, initial_offset=initial_offset)
    pad_name_lens = _decompose_straddle_pads(delta=target_header_offset - initial_offset)
    target_name = "STRADDLE"
    target_content = "STRADDLED\n"
    ext2_start = compute_directory_sector(image_path=str(image))
    with tempfile.TemporaryDirectory() as tmpdir:
        partition = Path(tmpdir) / "partition.ext2"
        subprocess.run(
            ["dd", f"if={image}", f"of={partition}", "bs=512", f"skip={ext2_start}", "status=none"],
            check=True,
        )
        script_lines: list[str] = []
        for index, name_len in enumerate(pad_name_lens):
            # Unique name of exact length `name_len`: 'p' * (name_len - 3)
            # then a 3-digit index — e.g. 'p000' (name_len=4), 'ppppp000'
            # (name_len=8), 'ppppppppp000' (name_len=12).
            pad_name = ("p" * (name_len - 3)) + f"{index:03d}"
            assert len(pad_name) == name_len, (pad_name, name_len)
            pad_path = Path(tmpdir) / pad_name
            pad_path.write_text("PAD\n")
            script_lines.append(f"write {pad_path} /bin/{pad_name}")
        target_path = Path(tmpdir) / target_name
        target_path.write_text(target_content)
        script_lines.append(f"write {target_path} /bin/{target_name}")
        script = "\n".join(script_lines) + "\n"
        result = subprocess.run(
            ["debugfs", "-w", str(partition)],
            input=script.encode(),
            capture_output=True,
            check=False,
        )
        stderr_text = result.stderr.decode()
        stderr_failed = any(
            line.strip() for line in stderr_text.splitlines() if not line.startswith("debugfs ") and "Allocated inode:" not in line
        )
        if result.returncode != 0 or stderr_failed:
            msg = f"debugfs straddle setup failed:\n{stderr_text}"
            raise RuntimeError(msg)
        subprocess.run(
            ["dd", f"if={partition}", f"of={image}", "bs=512", f"seek={ext2_start}", "conv=notrunc", "status=none"],
            check=True,
        )
    test.commands = [f"cat bin/{target_name}"]
    test.expect = r"^STRADDLED$"


def _bin_block0_first_block_num(*, debugfs_output: str) -> int:
    """Parse the first direct-block number from `debugfs stat <12>` output."""
    for line in debugfs_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("(0):"):
            return int(stripped[4:].split(",")[0].split(")")[0])
        if "(0)" in stripped and ":" in stripped:
            parts = stripped.replace("(0):", "").split(",")[0].strip()
            return int(parts.split()[0])
    msg = "could not find bin/ block 0 in debugfs stat output"
    raise RuntimeError(msg)


def _bin_block0_used_bytes(*, image: Path) -> int:
    """Byte-offset where the next entry would land within bin/'s block 0.

    Sums every entry's actual (header + padded-name) size — ignoring the
    last entry's rec_len padding to end-of-block — so the test's setup
    can insert an executable probe at a specific intra-block byte offset
    (e.g. anywhere past offset 512) and have it land in the sector the
    regression actually targets, not at offset 0 of a freshly-allocated
    next block where the bug wouldn't bite.
    """
    import struct  # noqa: PLC0415 — narrow-scope binary parsing helper

    block_size = _ext2_block_size(image=image)
    tmp_path = _ext2_extract(image=image)
    try:
        result = subprocess.run(
            ["debugfs", "-R", "stat <12>", str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        block_num = _bin_block0_first_block_num(debugfs_output=result.stdout)
        with tmp_path.open("rb") as f:
            f.seek(block_num * block_size)
            block_data = f.read(block_size)
    finally:
        tmp_path.unlink(missing_ok=True)

    used = 0
    offset = 0
    while offset + 8 <= block_size:
        _, rec_len, name_len, _ = struct.unpack_from("<IHBB", block_data, offset)
        if rec_len == 0:
            break
        actual = 8 + ((name_len + 1 + 3) & ~3)
        is_last_with_padding = rec_len > actual and offset + rec_len >= block_size
        used = offset + (actual if is_last_with_padding else rec_len)
        offset += rec_len
    return used


def _bin_dir_blocks(*, image: Path) -> int:
    """Return the number of 1 KB filesystem blocks bin/'s directory uses.

    debugfs reports the inode's Blockcount in 512-byte sectors, so divide.
    """
    tmp_path = _ext2_extract(image=image)
    try:
        result = subprocess.run(
            ["debugfs", "-R", "stat <12>", str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    for line in result.stdout.splitlines():
        if "Blockcount:" in line:
            sectors = int(line.split("Blockcount:")[1].split()[0])
            return sectors // 2
    msg = "could not parse Blockcount from debugfs stat <12>"
    raise RuntimeError(msg)


def _build_os(*, large_file: bool, temporary_directory: Path, block_size: int = 1024) -> None:
    """Run make_os.sh --ext2; abort if the build fails.

    Bumps the inode count to 1024 so the exec_first_middle_last test can
    pad bin/ to use all 12 of an ext2 directory inode's direct blocks
    (the lookup ceiling — ext2_search_dir doesn't follow indirect-block
    pointers).  At the default mke2fs inode ratio our 1.44 MB image only
    gets ~176 inodes; padding to 12 blocks needs ~770.  1024 inodes adds
    ~250 KB of inode-table metadata to the image; data-block space stays
    comfortably above what every other test (incl. doubly_indirect) needs.
    """
    image = temporary_directory / BASE_IMAGE
    result = subprocess.run(
        ["./make_os.sh", "--ext2", f"--ext2-block-size={block_size}", "--ext2-inode-count=1024", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(1)
    if large_file and block_size == 1024:
        _add_large_test_file(image=image)


def _decompose_straddle_pads(*, delta: int) -> list[int]:
    """Return name_lens for a chain of pads whose rec_lens sum to delta.

    ext2 rec_len comes in steps of 4 starting at 12 (= 8-byte header
    + name padded to a 4-byte boundary), so {12, 16, 20} via name_len
    {4, 8, 12} composes any multiple of 4 ≥ 12 — and delta is one by
    construction (see :func:`_pick_straddle_target_offset`).
    """
    name_lens: list[int] = []
    remaining = delta
    while remaining > 0:
        if remaining == 12 or remaining > 20:
            name_lens.append(4)
            remaining -= 12
        elif remaining == 16:
            name_lens.append(8)
            remaining -= 16
        else:  # remaining == 20
            name_lens.append(12)
            remaining -= 20
    return name_lens


def _ext2_block_size(*, image: Path) -> int:
    """Return the ext2 filesystem's block size in bytes."""
    tmp_path = _ext2_extract(image=image)
    try:
        result = subprocess.run(
            ["dumpe2fs", "-h", str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    for line in result.stdout.splitlines():
        if line.startswith("Block size:"):
            return int(line.split(":")[1].strip())
    msg = "could not parse Block size from dumpe2fs output"
    raise RuntimeError(msg)


def _ext2_extract(*, image: Path) -> Path:
    """Copy the ext2 partition out of `image` into a standalone temp file."""
    ext2_offset = compute_directory_sector(image_path=str(image)) * 512
    with image.open("rb") as f:
        f.seek(ext2_offset)
        ext2_data = f.read()
    fd, tmp_name = tempfile.mkstemp(suffix=".ext2")
    with os.fdopen(fd, "wb") as out:
        out.write(ext2_data)
    return Path(tmp_name)


def _fsck(*, image: Path) -> str | None:
    """Run e2fsck on the ext2 partition; return an error string or None if clean."""
    ext2_offset = compute_directory_sector(image_path=str(image)) * 512
    with Path(image).open("rb") as f:
        f.seek(ext2_offset)
        ext2_data = f.read()
    with tempfile.NamedTemporaryFile(suffix=".ext2", delete=False) as tmp:
        tmp.write(ext2_data)
        ext2_path = Path(tmp.name)
    try:
        result = subprocess.run(
            ["e2fsck", "-f", "-n", str(ext2_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            lines = result.stdout.splitlines()
            for line in lines:
                if line and not line.startswith("Pass ") and not line.startswith("Running ") and not line.startswith("/tmp"):
                    return line
            return f"exit {result.returncode}"
        return None
    finally:
        ext2_path.unlink(missing_ok=True)


def _pad_bin_to_full_directory(*, image: Path, test: ProgramTest) -> None:
    """Pad bin/ with empty fillers + exec probes until 12 direct blocks are full.

    12 direct blocks is the upper bound for a directory ext2_search_dir
    can walk — indirect-block traversal isn't implemented.  Three
    executable probes land in distinct blocks so a lookup of any of
    them necessarily walks both sectors of at least one block:

      _zexec_a:    inserted once block 0 has filled past byte 512 →
                   the probe lives in sector 1 of block 0 and its
                   lookup forces ext2_search_blk to walk past the
                   sector boundary inside that block.
      _zexec_b:    inserted after bin/ has grown to ~half the cap →
                   somewhere in block 6 or 7.
      _zexec_last: inserted after bin/ reaches the 12-block ceiling →
                   the literal final directory entry.

    Fillers are inserted in batches of `batch_size` (one debugfs session
    per batch, instead of one per filler).  After each batch we re-check
    the directory state; a batch may overshoot the threshold by up to
    `batch_size - 1` fillers — fine, the test only needs ≥ the threshold.

    The test's commands and expected regex are written here, post-setup,
    so the probe names the test asserts stay pinned to the names this
    helper actually wrote — robust to PROGRAMS growing in make_os.sh.
    """
    batch_size = 32
    hard_limit = 1500
    filler_index = 0

    def add_batch() -> None:
        nonlocal filler_index
        if filler_index + batch_size > hard_limit:
            msg = "filler limit hit"
            raise RuntimeError(msg)
        names = [f"_pad{filler_index + offset:04d}" for offset in range(batch_size)]
        add_empty_files(image_path=str(image), names=names, subdirectory="bin")
        filler_index += batch_size

    # Add empty fillers until bin's block 0 has filled past byte 512 —
    # i.e. it has spilled into sector 1.  The next inserted entry lands
    # past byte 512 too, so _zexec_a's lookup necessarily forces
    # ext2_search_blk to walk sector 1 of block 0 and read it as the
    # entry actually living there — the exact path the fix targets.
    while _bin_block0_used_bytes(image=image) < 768:
        add_batch()
    _add_exec_probe(image=image, name="_zexec_a")

    # Pad the directory until it spans roughly half its 12-block ceiling,
    # then insert _zexec_b — a probe in a "middle" block.  Then pad to
    # the full 12-block cap before adding _zexec_last so the final probe
    # is the literal last entry of the literal last walkable block.
    while _bin_dir_blocks(image=image) < EXT2_DIRECT_BLOCKS // 2:
        add_batch()
    _add_exec_probe(image=image, name="_zexec_b")

    while _bin_dir_blocks(image=image) < EXT2_DIRECT_BLOCKS:
        add_batch()
    _add_exec_probe(image=image, name="_zexec_last")

    test.commands = ["arp", "_zexec_a", "_zexec_b", "_zexec_last"]
    test.expect = (
        r"usage: arp <ip>"
        r"[\s\S]+^EXEC _zexec_a$"
        r"[\s\S]+^EXEC _zexec_b$"
        r"[\s\S]+^EXEC _zexec_last$"
    )


def _pick_straddle_target_offset(*, block_size: int, initial_offset: int) -> int:
    """Return the smallest reachable header offset whose entry name straddles.

    Header lands at boundary - 8: header bytes occupy the lo 512-byte
    sector, the 8-byte name lives in the hi sector.  We need a boundary
    whose distance from initial_offset is at least 12 (room for one
    pad of minimum rec_len) and a multiple of 4 (rec_len granularity).
    """
    for boundary in range(512, block_size, 512):
        delta = boundary - 8 - initial_offset
        if delta >= 12 and delta % 4 == 0:
            return boundary - 8
    msg = f"no usable straddle boundary: block_size={block_size}, initial_offset={initial_offset}"
    raise RuntimeError(msg)


def _run_suite(
    *,
    fail_fast: bool,
    floppy: bool,
    tests: list[ProgramTest],
    temporary_directory: Path,
    label: str = "",
) -> tuple[int, int, list[str]]:
    """Run a list of ProgramTests; return (pass_count, fail_count, failed_names)."""
    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    for test in tests:
        name = f"{label}{test.name}" if label else test.name
        ok, message, boot_time, command_time = _run_test(
            floppy=floppy,
            temporary_directory=temporary_directory,
            test=test,
        )
        timing = f"boot {boot_time:.2f}s  cmd {command_time:.2f}s"
        if ok:
            print(f"  PASS  {name:<20}              {timing}")
            pass_count += 1
        else:
            print(f"  FAIL  {name:<20}  {message}   {timing}")
            fail_count += 1
            failed.append(name)
            if fail_fast:
                break
    return pass_count, fail_count, failed


def _run_test(*, floppy: bool, temporary_directory: Path, test: ProgramTest) -> tuple[bool, str, float, float]:
    """Run one ProgramTest; return (passed, message, boot_time, command_time)."""
    test_image = temporary_directory / f"test_{test.name}.img"
    shutil.copy2(temporary_directory / BASE_IMAGE, test_image)
    if test.setup is not None:
        test.setup(test_image, test)
    try:
        result = run_commands(
            test.commands,
            command_timeout=test.timeout,
            drive=test_image,
            floppy=floppy,
            snapshot=False,
        )
    except TimeoutError as error:
        return False, f"timeout: {error}", 0.0, 0.0
    except RuntimeError as error:
        return False, f"qemu error: {error}", 0.0, 0.0
    command_time = sum(result.command_times)
    failures = []
    if not re.search(test.expect, result.output.replace("\r", ""), re.MULTILINE):
        failures.append(f"expected regex {test.expect!r} not found in output")
    fsck_error = _fsck(image=test_image)
    if fsck_error:
        failures.append(f"fsck: {fsck_error}")
    return (not failures), "; ".join(failures), result.boot_time, command_time


# Subset of tests to re-run with 2 KB blocks (exercises the variable-block-size paths).
# Excludes tests that don't touch ext2 (echo, hello, uptime).
BLOCK_SIZE_TESTS: list[ProgramTest] = [
    t
    for t in TESTS
    if t.name
    in {
        "cat",
        "cat_large",
        "chmod",
        "cp",
        "cp_into_subdir",
        "cp_overwrite_shrink",
        "exec_first_middle_last",
        "ls",
        "mkdir",
        "mkdir_ls_root",
        "mkdir_nested",
        "multi_sector_dir",
        "rename",
        "rename_dir",
        "rm",
        "rmdir",
        "rmdir_nonempty",
    }
] + [
    # 2 KB-only tests — exercises in-block boundaries (1024, 1536) that
    # 1 KB blocks can't reach because bin/'s baseline entries already
    # extend past 504, leaving no room to stage a straddle there.
    ProgramTest(
        # `_add_straddle_dir_filler` chains filler entries so STRADDLE's
        # 8-byte header ends exactly at a 512-byte sector boundary,
        # putting its name in the next sector.  ext2_search_blk's name
        # compare has to read across the boundary; a regression that
        # uses only the lo half of its sliding window matches against
        # stale buffer bytes and reports the entry missing.  Stronger
        # guarantee than multi_sector_dir, which only happens to
        # straddle for particular block_size + bin/ layouts.
        "straddle_dir",
        commands=[],
        expect="",
        setup=lambda image, test: _add_straddle_dir_filler(image=image, test=test),
    ),
]


def main() -> int:
    """Run the selected ProgramTests and print a summary."""
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="restrict to one program (e.g. 'hello')")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first failing test",
    )
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="boot QEMU with the drive attached as a floppy (if=floppy); "
        "skips the 2 KB-block-size matrix because the resulting image "
        "exceeds the 1.44 MB floppy capacity",
    )
    parser.add_argument(
        "--slow",
        action="store_true",
        help="include slow tests (large-file and doubly-indirect I/O)",
    )
    arguments = parser.parse_args()

    tests = [t for t in TESTS if arguments.program is None or t.name == arguments.program]
    if not tests:
        print(f"No test named {arguments.program!r}")
        return 1

    if arguments.program is None and not arguments.slow:
        for test in tests:
            if test.slow:
                print(f"  SKIP  {test.name:<20} (slow; pass --slow to include)")
        tests = [t for t in tests if not t.slow]

    total_pass = 0
    total_fail = 0
    all_failed: list[str] = []

    with tempfile.TemporaryDirectory(prefix="test_ext2_") as temporary_path:
        temporary_directory = Path(temporary_path)
        _build_os(large_file=arguments.slow, temporary_directory=temporary_directory, block_size=1024)
        p, f, failed = _run_suite(
            fail_fast=arguments.fail_fast, floppy=arguments.floppy, tests=tests, temporary_directory=temporary_directory
        )
        total_pass += p
        total_fail += f
        all_failed += failed

    # 2 KB block-size tests (only when running the full suite, and not under --floppy:
    # mke2fs grows a 2 KB-block image past 1.44 MB so it can't be addressed via
    # QEMU's floppy backend).
    if arguments.program is None and not arguments.floppy and not (arguments.fail_fast and total_fail):
        blk2_tests = BLOCK_SIZE_TESTS if arguments.slow else [t for t in BLOCK_SIZE_TESTS if not t.slow]
        with tempfile.TemporaryDirectory(prefix="test_ext2_2k_") as temporary_path:
            temporary_directory = Path(temporary_path)
            _build_os(large_file=False, temporary_directory=temporary_directory, block_size=2048)
            p, f, failed = _run_suite(
                fail_fast=arguments.fail_fast,
                floppy=arguments.floppy,
                tests=blk2_tests,
                temporary_directory=temporary_directory,
                label="2k/",
            )
            total_pass += p
            total_fail += f
            all_failed += failed
    elif arguments.program is None and arguments.floppy:
        print(f"  SKIP  2k/* ({len(BLOCK_SIZE_TESTS)} tests) — image exceeds 1.44 MB floppy capacity")

    print()
    print(f"{total_pass} passed, {total_fail} failed")
    if total_fail:
        print("Failed:", " ".join(all_failed))
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
