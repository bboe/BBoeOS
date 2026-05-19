#!/usr/bin/env python3
"""Runtime smoke tests for user-space programs.

Boots bboeos in QEMU, runs a representative command for each test
program, and checks the output against an expected regex.  The
``--filesystem`` flag selects between bbfs (default) and ext2 builds;
ext2 runs additionally include the ext2-specific stress tests
(doubly-indirect blocks, multi-sector directory walks, rename across
parents, …) and an ``e2fsck -f -n`` integrity check after each test,
plus a 2 KB-block-size matrix re-run of the ext2-touching tests.

Skips ``shell`` (implicit) and ``asm`` (covered by test_asm.py).

Usage:
    ./test_programs.py                          # bbfs, full suite
    ./test_programs.py arp                      # one program (bbfs)
    ./test_programs.py --filesystem ext2        # ext2, full suite
    ./test_programs.py --filesystem ext2 cat    # one program (ext2)
    ./test_programs.py --slow                   # bbfs + bigbss tripwire
    ./test_programs.py --filesystem ext2 --slow # ext2 + bigbss + ext2 large-file / doubly-indirect
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_IMAGE = "drive.img"
_DEFAULT_PROGRAM_TIMEOUT = float(os.environ.get("BBOE_PROGRAM_TIMEOUT", "1.0"))
_LARGE_FILE_TIMEOUT = float(os.environ.get("BBOE_LARGE_FILE_TIMEOUT", "6.0"))
_DOUBLY_INDIRECT_TIMEOUT = float(os.environ.get("BBOE_DOUBLY_INDIRECT_TIMEOUT", "12.0"))

sys.path.insert(0, str(REPO_ROOT))

from run_qemu import run_commands  # noqa: E402

from add_file import (  # noqa: E402
    ENTRIES_PER_SECTOR,
    NAME_FIELD,
    OFFSET_SECTOR,
    SECTOR_SIZE,
    add_empty_files,
    add_file,
    compute_directory_sector,
    ext2_add_file,
    find_subdirectory_entry,
    iter_entries,
)

_ALL_FILESYSTEMS = frozenset({"bbfs", "ext2"})
_BBFS_DIRECTORY_SECTORS = 4
_BBFS_DIRECTORY_MAX_ENTRIES = _BBFS_DIRECTORY_SECTORS * ENTRIES_PER_SECTOR  # 64
_DOUBLY_INDIRECT_SENTINEL = b"EXT2_DOUBLY_INDIRECT_OK"
_DOUBLY_INDIRECT_START = (12 + 256) * 1024  # byte 274432 = first doubly-indirect block
_EXT2_DIRECT_BLOCKS = 12  # ext2 directory blocks ext2_search_dir walks (i_block[0..11])


@dataclass
class ProgramTest:
    """One runtime test: shell commands to run and a regex the output must match.

    ``filesystems`` is the set of backends this test applies to; tests
    that only make sense for one backend (e.g. the ext2-specific
    directory-walk stress tests) restrict it to a single entry.

    ``slow`` marks tests that take seconds-to-tens-of-seconds each (the
    bigbss tripwire trio at -m 2047/2048, the ext2 large-file and
    doubly-indirect stress tests); ``--slow`` on the runner opts them in.

    ``memory`` overrides ``run_commands``'s 1 MB default for tests whose
    program needs more (currently only the bigbss family).
    """

    name: str
    commands: list[str]
    expect: str
    setup: Callable[[Path, ProgramTest], None] | None = None
    extra_qemu_args: list[str] = field(default_factory=list)
    filesystems: frozenset[str] = field(default=_ALL_FILESYSTEMS)
    memory: str | None = None
    skip: str | None = None
    slow: bool = False
    timeout: float = _DEFAULT_PROGRAM_TIMEOUT
    with_net: bool = False


# ---------------------------------------------------------------------------
# Helpers shared by both filesystems
# ---------------------------------------------------------------------------


def _add_exec_probe(*, image: Path, name: str) -> None:
    """Compile a tiny C program that prints ``EXEC <name>`` and add it to bin/."""
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


# ---------------------------------------------------------------------------
# bbfs helpers (exec_first_middle_last setup)
# ---------------------------------------------------------------------------


def _bbfs_bin_entry_names(*, image: Path) -> list[str | None]:
    """Return bin/'s slot table as a list of length 48; empty slots are None."""
    image_data = bytearray(image.read_bytes())
    directory_sector = compute_directory_sector(image_path=str(image))
    bin_offset = find_subdirectory_entry(
        directory_sector=directory_sector,
        directory_sectors=_BBFS_DIRECTORY_SECTORS,
        image=image_data,
        name="bin",
    )
    if bin_offset is None:
        msg = "bin/ subdirectory not found in image"
        raise RuntimeError(msg)
    bin_start = int.from_bytes(image_data[bin_offset + OFFSET_SECTOR : bin_offset + OFFSET_SECTOR + 2], "little")
    return [
        bytes(image_data[entry_offset : entry_offset + NAME_FIELD]).rstrip(b"\x00").decode() if image_data[entry_offset] != 0 else None
        for entry_offset in iter_entries(base_offset=bin_start * SECTOR_SIZE, sector_count=_BBFS_DIRECTORY_SECTORS)
    ]


def _bbfs_pad_bin_to_full_directory(*, image: Path, test: ProgramTest) -> None:
    """Pad bin/ to BBfs's 64-entry cap with an executable probe written last.

    bbfs subdirectories don't carry . / ..; bin/ starts populated with
    the PROGRAMS list (count varies as PROGRAMS grows).  The setup
    counts the existing entries, adds (63 - existing) empty fillers
    in a single batched image flush, then writes _zexec_last as the
    literal final entry (slot 63, in sector 3 of bbfs's 4-sector
    directory).  Asserts arp (slot 0, sector 0), a runtime-picked
    sector-1 entry (slots 16..31, name chosen from the post-padding
    bin/ layout so the test stays robust to PROGRAMS reordering), and
    _zexec_last (slot 63, sector 3) all resolve so the lookup walks
    all four of bbfs's directory sectors.
    """
    names = _bbfs_bin_entry_names(image=image)
    used = sum(1 for name in names if name is not None)
    if (fillers_needed := _BBFS_DIRECTORY_MAX_ENTRIES - used - 1) >= 0:
        add_empty_files(
            image_path=str(image),
            names=[f"_pad{filler_index:02d}" for filler_index in range(fillers_needed)],
            subdirectory="bin",
        )
        _add_exec_probe(image=image, name="_zexec_last")
        names = _bbfs_bin_entry_names(image=image)
        last_name = "_zexec_last"
        last_expect = r"^EXEC _zexec_last$"
    else:
        # bin/ is already at the bbfs 64-entry cap from USER_PROGRAMS +
        # tests/programs/ alone — no room to drop a synthetic
        # _zexec_last.  Pick a natural sector-3 entry (slots 48..63)
        # whose program has a runnable TESTS entry for the lookup probe.
        sector_3_start = 3 * ENTRIES_PER_SECTOR
        sector_3_end = 4 * ENTRIES_PER_SECTOR
        last_name, last_expect = _bbfs_pick_sector_probe(
            names=names,
            slot_end=sector_3_end,
            slot_start=sector_3_start,
        )

    middle_name, middle_expect = _bbfs_pick_sector1_probe(names=names)
    test.commands = ["arp", middle_name, last_name]
    test.expect = (
        r"usage: arp <ip>"
        rf"[\s\S]+{middle_expect}"
        rf"[\s\S]+{last_expect}"
    )


def _bbfs_pick_sector1_probe(*, names: list[str | None]) -> tuple[str, str]:
    """Return (program_name, expected_regex) for some entry in sector 1.

    Sector 1 spans slots 16..31.  Convenience wrapper around
    :func:`_bbfs_pick_sector_probe`.
    """
    return _bbfs_pick_sector_probe(
        names=names,
        slot_end=2 * ENTRIES_PER_SECTOR,
        slot_start=ENTRIES_PER_SECTOR,
    )


def _bbfs_pick_sector_probe(*, names: list[str | None], slot_end: int, slot_start: int) -> tuple[str, str]:
    """Return (program_name, expected_regex) for a slot in [slot_start, slot_end).

    Walks the range in order and picks the first whose program has a
    single-command, non-network, bbfs-eligible entry in TESTS so its
    expected output regex is reusable here.  Used by the
    ``exec_first_middle_last`` setup to verify the directory walk
    reaches every sector of bbfs's 4-sector bin/.
    """
    runnable = {
        test.name: test.expect
        for test in TESTS
        if test.commands == [test.name] and not test.with_net and test.setup is None and test.skip is None and "bbfs" in test.filesystems
    }
    for slot in range(slot_start, slot_end):
        name = names[slot]
        if name is not None and name in runnable:
            return name, runnable[name]
    msg = f"no testable program in bin/'s slots {slot_start}..{slot_end - 1}; update TESTS"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# ext2 helpers (large-file injection, fsck, directory-walk setups)
# ---------------------------------------------------------------------------


def _ext2_add_large_test_file(*, image: Path) -> None:
    """Inject a 280 KB file into src/ to exercise the doubly-indirect block paths.

    With 1 KB blocks the doubly-indirect threshold is 268 KB (12 direct
    + 256 singly-indirect).  280 KB puts 12 data blocks into the
    doubly-indirect region, covering both the allocation and free paths.

    A sentinel string is written at byte 274432 (start of block 268,
    the first doubly-indirect block) so tests can confirm reads and
    writes actually reach the doubly-indirect region rather than
    matching content in the direct or singly-indirect range.
    """
    target_bytes = 280 * 1024
    source = (REPO_ROOT / "src" / "c" / "asm.c").read_bytes()
    content = bytearray((source * (target_bytes // len(source) + 1))[:target_bytes])
    content[_DOUBLY_INDIRECT_START : _DOUBLY_INDIRECT_START + len(_DOUBLY_INDIRECT_SENTINEL)] = _DOUBLY_INDIRECT_SENTINEL
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


def _ext2_add_multi_sector_dir_filler(*, image: Path, test: ProgramTest) -> None:
    """Pad bin/ until an entry lands in block 0's final 512-byte sector.

    ext2_search_blk reads one 512-byte sector at a time and walks the
    entries inside it; on a miss it bumps the within-block sector index
    and reads the next sector.  A regression that loses the sector
    counter or the block number across iterations only surfaces when a
    target entry actually lives past byte 512 of its block.  1 KB
    blocks span two sectors (boundary at 512); 2 KB blocks span four
    (boundaries at 512, 1024, 1536), so a fixed handful of stubs enough
    to cross the first boundary on 1 KB blocks does not exercise the
    1→2 or 2→3 advances on 2 KB blocks.
    """
    block_size = _ext2_block_size(image=image)
    last_sector_start = block_size - 512
    initial_offset = _ext2_bin_block0_used_bytes(image=image)
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


def _ext2_add_straddle_dir_filler(*, image: Path, test: ProgramTest) -> None:
    """Place an entry whose 8-byte name spans a 512-byte sector boundary.

    Pads bin/ with a chain of filler entries (rec_lens 12 / 16 / 20 via
    name_lens 4 / 8 / 12) so the next entry — STRADDLE, name_len 8 —
    has its header at offset boundary - 8 of bin/'s first block: header
    in the lo 512-byte sector, name in the hi sector.  Looking it up
    forces ext2_search_blk's name compare to read across the 512-byte
    boundary; a regression that uses only the lo half of its sliding
    window compares against stale bytes and reports the entry missing.
    """
    block_size = _ext2_block_size(image=image)
    initial_offset = _ext2_bin_block0_used_bytes(image=image)
    target_header_offset = _ext2_pick_straddle_target_offset(block_size=block_size, initial_offset=initial_offset)
    pad_name_lens = _ext2_decompose_straddle_pads(delta=target_header_offset - initial_offset)
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


def _ext2_bin_block0_first_block_num(*, debugfs_output: str) -> int:
    """Parse the first direct-block number from ``debugfs stat <12>`` output."""
    for line in debugfs_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("(0):"):
            return int(stripped[4:].split(",")[0].split(")")[0])
        if "(0)" in stripped and ":" in stripped:
            parts = stripped.replace("(0):", "").split(",")[0].strip()
            return int(parts.split()[0])
    msg = "could not find bin/ block 0 in debugfs stat output"
    raise RuntimeError(msg)


def _ext2_bin_block0_used_bytes(*, image: Path) -> int:
    """Byte-offset where the next entry would land within bin/'s block 0."""
    block_size = _ext2_block_size(image=image)
    tmp_path = _ext2_extract(image=image)
    try:
        result = subprocess.run(
            ["debugfs", "-R", "stat <12>", str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        block_num = _ext2_bin_block0_first_block_num(debugfs_output=result.stdout)
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


def _ext2_bin_dir_blocks(*, image: Path) -> int:
    """Return the number of 1 KB filesystem blocks bin/'s directory uses."""
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


def _ext2_decompose_straddle_pads(*, delta: int) -> list[int]:
    """Return name_lens for a chain of pads whose rec_lens sum to delta.

    ext2 rec_len comes in steps of 4 starting at 12 (= 8-byte header
    + name padded to a 4-byte boundary), so {12, 16, 20} via name_len
    {4, 8, 12} composes any multiple of 4 ≥ 12.
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


def _ext2_extract(*, image: Path) -> Path:
    """Copy the ext2 partition out of *image* into a standalone temp file."""
    ext2_offset = compute_directory_sector(image_path=str(image)) * 512
    with image.open("rb") as f:
        f.seek(ext2_offset)
        ext2_data = f.read()
    fd, tmp_name = tempfile.mkstemp(suffix=".ext2")
    with os.fdopen(fd, "wb") as out:
        out.write(ext2_data)
    return Path(tmp_name)


def _ext2_fsck(*, image: Path) -> str | None:
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
            for line in result.stdout.splitlines():
                if line and not line.startswith("Pass ") and not line.startswith("Running ") and not line.startswith("/tmp"):
                    return line
            return f"exit {result.returncode}"
        return None
    finally:
        ext2_path.unlink(missing_ok=True)


def _ext2_pad_bin_to_full_directory(*, image: Path, test: ProgramTest) -> None:
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

    while _ext2_bin_block0_used_bytes(image=image) < 768:
        add_batch()
    _add_exec_probe(image=image, name="_zexec_a")

    while _ext2_bin_dir_blocks(image=image) < _EXT2_DIRECT_BLOCKS // 2:
        add_batch()
    _add_exec_probe(image=image, name="_zexec_b")

    while _ext2_bin_dir_blocks(image=image) < _EXT2_DIRECT_BLOCKS:
        add_batch()
    _add_exec_probe(image=image, name="_zexec_last")

    test.commands = ["arp", "_zexec_a", "_zexec_b", "_zexec_last"]
    test.expect = (
        r"usage: arp <ip>"
        r"[\s\S]+^EXEC _zexec_a$"
        r"[\s\S]+^EXEC _zexec_b$"
        r"[\s\S]+^EXEC _zexec_last$"
    )


def _ext2_pick_straddle_target_offset(*, block_size: int, initial_offset: int) -> int:
    """Return the smallest reachable header offset that straddles a 512-byte sector boundary inside a block.

    Block-boundary candidates (boundary % block_size == 0) are skipped:
    ext2 forbids directory entries from spanning a block boundary, so a
    straddle there would just be invalid layout.  We search up to four
    blocks ahead so the test still works when bin/ has grown enough to
    push initial_offset past the first block's mid-sector.
    """
    for boundary in range(512, 4 * block_size, 512):
        if boundary % block_size == 0:
            continue
        delta = boundary - 8 - initial_offset
        if delta >= 12 and delta % 4 == 0:
            return boundary - 8
    msg = f"no usable straddle boundary: block_size={block_size}, initial_offset={initial_offset}"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Test catalogue
# ---------------------------------------------------------------------------

_BBFS_ONLY = frozenset({"bbfs"})
_EXT2_ONLY = frozenset({"ext2"})


TESTS: list[ProgramTest] = [
    ProgramTest("alarm_cancel", ["alarm_test cancel"], r"^CANCEL_OK prev=(4[0-9]|50)$"),
    ProgramTest("alarm_coalesce", ["alarm_test coalesce"], r"^COALESCE_OK count=(\d|1[0-2])$"),
    ProgramTest("alarm_during_sleep", ["alarm_test during_sleep"], r"^EINTR_OK elapsed=(4[0-9]|[5-9][0-9]|100)$"),
    ProgramTest(
        "alarm_default_kill",
        ["alarm_test default_kill", "echo recovered"],
        r"ARMING[\s\S]*\^A[\s\S]*recovered",
    ),
    ProgramTest("alarm_nesting", ["alarm_test nesting\n\x03"], r"^NESTED_OK$"),
    ProgramTest("alarm_oneshot", ["alarm_test oneshot"], r"^ALARM_OK$"),
    ProgramTest("alarm_repeat", ["alarm_test repeat"], r"^REPEAT_OK count=(8|9|1[0-2])$"),
    ProgramTest("arp", ["arp 10.0.2.2"], r"10\.0\.2\.2 is at [0-9A-F:]+", with_net=True),
    # audio_open: open /dev/audio, sleep ~600 ms while SB16 fires several
    # half-buffer IRQ 5s, close.  Without -device sb16 the open returns -1.
    ProgramTest(
        "audio_open",
        ["audio_test open"],
        r"^audio_open: fd=\d+[\s\S]*audio_open: closed cleanly$",
        extra_qemu_args=["-audiodev", "none,id=a", "-device", "sb16,audiodev=a"],
        timeout=3.0,
    ),
    # audio_tone: write 6 x 2048-byte chunks of a 1.1 kHz square wave to
    # /dev/audio.  Each write should return 2048 once the ring drains via
    # IRQ 5; final "closed" line confirms a clean exit.
    ProgramTest(
        "audio_tone",
        ["audio_test tone"],
        r"audio_tone: write 5 returned 2048[\s\S]*audio_tone: closed$",
        extra_qemu_args=["-audiodev", "none,id=a", "-device", "sb16,audiodev=a"],
        timeout=5.0,
    ),
    # Maximum-BSS success case AND kmap-window smoke test.  bigbss
    # declares BIGBSS_PAGES (see tests/programs/bigbss_size.h) = 523,551 of
    # BSS at -m 2048 — large enough that ~half the frames sit
    # above FRAME_DIRECT_MAP_LIMIT (~1020 MB).  program_enter's
    # phase-2 zero-fills those high frames through the kmap window
    # (memory_management/kmap.asm), so a successful run validates
    # kmap_map / kmap_unmap end-to-end.  The verify pass after the
    # write loop catches any kmap zero-fill that lands at the wrong
    # phys.
    ProgramTest(
        "bigbss",
        ["bigbss"],
        r"^bigbss: OK$",
        filesystems=_BBFS_ONLY,
        memory="2048",
        slow=True,
        timeout=180.0,
    ),
    # Tripwire-low: same program at -m 2047 (one MB less RAM, ~256
    # fewer frames in the bitmap).  At -m 2047 BIGBSS_PAGES + per-PD
    # overhead no longer fits, and program_enter OOMs partway
    # through phase 2 (also exercising address_space_destroy on a
    # partially-built PD whose user PTs landed both below and above
    # the direct-map ceiling).  Asserts the OOM message AND a
    # follow-up `echo hello` runs in the respawned shell.
    ProgramTest(
        "bigbss_oom",
        ["bigbss", "echo hello"],
        r"^exec: out of memory$[\s\S]+^hello$",
        filesystems=_BBFS_ONLY,
        memory="2047",
        slow=True,
        timeout=120.0,
    ),
    # Tripwire-high: bigbss_fail declares BIGBSS_PAGES + 1 of BSS —
    # exactly one page beyond what bigbss fits at -m 2048 — and
    # asserts OOM.  Page-precise: any upward drift in BIGBSS_PAGES
    # makes this fit and the test fails (no OOM message).
    ProgramTest(
        "bigbss_fail",
        ["bigbss_fail", "echo hello"],
        r"^exec: out of memory$[\s\S]+^hello$",
        filesystems=_BBFS_ONLY,
        memory="2048",
        slow=True,
        timeout=60.0,
    ),
    ProgramTest("bits", ["codegen_test bits"], r"^b-=  = 46$"),
    ProgramTest("booltest", ["codegen_test bool"], r"^sum      = 3$"),
    ProgramTest("cat", ["cat src/parse_ip.asm"], r"^parse_ip:"),
    ProgramTest(
        "cat_large",
        ["cat src/large.bin"],
        r"Self-hosted x86 assembler",
        filesystems=_EXT2_ONLY,
        slow=True,
        timeout=_LARGE_FILE_TIMEOUT,
    ),
    ProgramTest("cat_stdin", ["echo piped | cat"], r"^piped$"),
    ProgramTest("cftest", ["convention_test carry"], r"tick\(\) fired 3 times, remaining = 0"),
    ProgramTest("chmod", ["chmod +x arp"], r"\$"),
    ProgramTest("cp", ["cp src/parse_ip.asm tmpb", "ls"], r"tmpb"),
    ProgramTest(
        "cp_into_subdir",
        ["mkdir mydir", "cp src/parse_ip.asm mydir/copy.asm", "cat mydir/copy.asm"],
        r"^parse_ip:",
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        "cp_overwrite_shrink",
        ["cp src/asm.c out.c", "cp src/parse_ip.asm out.c", "cat out.c"],
        r"^parse_ip:",
        filesystems=_EXT2_ONLY,
        timeout=_LARGE_FILE_TIMEOUT,
    ),
    # sleep_forever calls read(STDIN, buffer, 1) which blocks until a byte
    # arrives.  The embedded \x03 (Ctrl+C) is detected by fd_read_console,
    # which sets pending_sigint and returns.  The syscall epilogue's
    # SIGNAL_TAIL_CHECK dispatches SIGINT and routes through child_terminate
    # with EAX = SIGINT (2).  expand_dollar_question maps wait-status 0x0002
    # to bash_status = 128 + 2 = 130, which echo $? then prints.
    ProgramTest(
        "ctrl_c_into_sleep",
        ["sleep_forever\n\x03", "echo $?"],
        r"\^C[\s\S]*\b130\b",
        timeout=3.0,
    ),
    # Three calls in a row must agree on the date — catches DX-clobber-style
    # bugs where consecutive RTC reads return drifting / mismatched values.
    ProgramTest(
        "date",
        ["date", "date", "date"],
        r"(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}[\s\S]*?\1 \d{2}:\d{2}:\d{2}[\s\S]*?\1 \d{2}:\d{2}:\d{2}",
    ),
    ProgramTest("dns", ["dns example.com"], r"example\.com is at \d+\.\d+\.\d+\.\d+", with_net=True, timeout=30.0),
    ProgramTest(
        "doubly_indirect_cat",
        ["cat src/large.bin"],
        r"EXT2_DOUBLY_INDIRECT_OK",  # sentinel placed at byte 274432 (block 268)
        filesystems=_EXT2_ONLY,
        slow=True,
        timeout=_DOUBLY_INDIRECT_TIMEOUT,
    ),
    ProgramTest(
        "doubly_indirect_cp",
        ["cp src/large.bin out.bin", "cat out.bin"],
        r"EXT2_DOUBLY_INDIRECT_OK",  # verifies doubly-indirect write path
        filesystems=_EXT2_ONLY,
        slow=True,
        timeout=_DOUBLY_INDIRECT_TIMEOUT,
    ),
    ProgramTest(
        "doubly_indirect_cp_shrink",
        ["cp src/large.bin out.bin", "cp src/parse_ip.asm out.bin", "cat out.bin"],
        r"^parse_ip:",
        filesystems=_EXT2_ONLY,
        slow=True,
        timeout=_DOUBLY_INDIRECT_TIMEOUT,
    ),
    # 'draw\nq' runs `draw`, then draw reads the trailing 'q' from the
    # serial buffer and exits its main loop (back to text mode).  draw
    # has no serial output of its own — all writes go to VGA — so the
    # follow-up `echo hello` is what the regex matches: if draw crashed
    # or left the shell wedged in graphics mode, echo would never run.
    # See the `edit` entry below for the same pattern with Ctrl+Q.
    ProgramTest("draw", ["draw\nq", "echo hello"], r"^\$ draw[\s\S]+^hello$"),
    ProgramTest("echo", ["echo foo bar baz"], r"^foo bar baz$"),
    ProgramTest("echo_many_args", ["echo a b c d e", "ls"], r"^a b c d e$"),
    # 'edit hello\n\x11' runs `edit hello`, then edit consumes the trailing
    # Ctrl+Q (\x11) from the serial buffer.  hello doesn't exist in cwd, so
    # edit opens with an empty buffer; with dirty=0 a single Ctrl+Q exits.
    # The follow-up `echo hello` command confirms the shell is fully
    # functional again — catches PD teardown / VGA mode reset bugs that
    # would otherwise leave the shell wedged.  Doubles as a regression
    # for the 448 KB BSS allocation in the per-program PD.
    # 4 MB: B5 keeps the parent shell's PD alive while the child PD is
    # being built, so both are resident simultaneously.  At 1 MB the
    # ~80 KB shell PD + 448 KB edit BSS exhausts the bitmap allocator
    # and program_enter returns OOM.
    ProgramTest(
        "edit",
        ["edit hello\n\x11", "echo hello"],
        r"^hello  line 1  col 1[\s\S]+^hello$",
        memory="4",
    ),
    # Pad bin/ with empty fillers until BBfs's 48-entry cap is hit,
    # ending with an executable probe so the final directory entry
    # is something we can exec.  Asserts arp (slot 0), a runtime-picked
    # sector-1 entry, and _zexec_last (slot 47) all resolve.
    ProgramTest(
        "exec_first_middle_last",
        commands=[],
        expect="",
        filesystems=_BBFS_ONLY,
        setup=_bbfs_pad_bin_to_full_directory,
    ),
    # Pad bin/ until its inode uses all 12 direct blocks, interleaving
    # three executable probes — _zexec_a (block 1 first entry), _zexec_b
    # (~middle of the directory), _zexec_last (literal final entry).
    ProgramTest(
        "exec_first_middle_last",
        commands=[],
        expect="",
        filesystems=_EXT2_ONLY,
        setup=_ext2_pad_bin_to_full_directory,
    ),
    # exit_status runs _exit(N) and the shell encodes N into bits 15..8 of
    # the wait status.  expand_dollar_question extracts WEXITSTATUS and
    # echo $? prints the original value.
    # Linux-style argv layout: argv[0] = basename, argc counts it,
    # argv[argc] = NULL.  argv_basename.c prints all four to confirm.
    ProgramTest(
        "argv_basename",
        ["argv_basename alpha bravo"],
        r"argc=3\nargv\[0\]=argv_basename\nargv\[1\]=alpha\nargv\[2\]=bravo\nargv\[argc\]=NULL",
    ),
    ProgramTest("echo_basic", ["echo hello world"], r"^hello world\r?\n\$"),
    ProgramTest("echo_dash_e", ["echo -e a\\nb\\tc"], r"^a\r?\nb\tc\r?\n\$"),
    ProgramTest("echo_dquote_strip", ['echo "one two"'], r"^one two\r?\n\$"),
    ProgramTest("echo_squote_preserves_double", ["echo 'a\"b'"], r"^a\"b\r?\n\$"),
    ProgramTest("echo_squote_strip", ["echo 'one two'"], r"^one two\r?\n\$"),
    ProgramTest("exit_status_zero", ["exit_status 0", "echo $?"], r"echo \$\?\n0\n"),
    ProgramTest("exit_status_42", ["exit_status 42", "echo $?"], r"echo \$\?\n42\n"),
    ProgramTest("false_chain", ["false && echo skipped || echo ran"], r"^ran$"),
    ProgramTest("fctest", ["convention_test regparm"], r"accumulate\(9\)    = 28"),
    ProgramTest("gptest", ["fault_test gp", "echo recovered"], r"EXC0D[\s\S]*recovered"),
    ProgramTest("grep_basic", ["echo -e aaa\\nbbb\\naaa | grep aaa"], r"^aaa\r?\naaa\r?\n\$"),
    ProgramTest("grep_case", ["echo HELLO | grep -i hello"], r"^HELLO\r?\n\$"),
    ProgramTest("grep_inverse", ["echo -e aaa\\nbbb\\naaa | grep -v aaa"], r"^bbb\r?\n\$"),
    ProgramTest("grep_no_match", ["echo hi | grep nope; echo $?"], r"^1$"),
    ProgramTest("grep_number", ["echo -e aaa\\nbbb\\naaa | grep -n aaa"], r"^1:aaa\r?\n3:aaa\r?\n\$"),
    ProgramTest("head_basic", ["seq 1 5 | head -n 2"], r"^1$\n^2$"),
    ProgramTest("head_default", ["seq 1 20 | head"], r"^1$\n^2$\n^3$\n^4$\n^5$\n^6$\n^7$\n^8$\n^9$\n^10$"),
    ProgramTest("loop", ["loop_test basic"], r"aaaaa"),
    ProgramTest("loop_array", ["loop_test array"], r"abc"),
    ProgramTest("ls", ["ls bin"], r"^arp$"),
    ProgramTest("mkdir", ["mkdir tmpd", "ls"], r"tmpd/"),
    ProgramTest(
        "mkdir_ls_root",
        ["mkdir mydir", "ls"],
        r"mydir/",
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        "mkdir_nested",
        ["mkdir parent", "mkdir parent/child", "ls parent/child"],
        r"^\.\./",
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        # `_ext2_add_multi_sector_dir_filler` (run as a per-test setup)
        # keeps appending _zzpadNN stubs to bin/ until one lands in the
        # *last* 512-byte sector of bin/'s first directory block — byte
        # ≥ 512 on 1 KB blocks, byte ≥ 1536 on 2 KB blocks — then writes
        # the name of that probe into commands+expect.  Confirms
        # ext2_search_blk advances across every intra-block sector
        # boundary (0→1 on 1 KB; 0→1→2→3 on 2 KB).
        "multi_sector_dir",
        commands=[],
        expect="",
        filesystems=_EXT2_ONLY,
        setup=_ext2_add_multi_sector_dir_filler,
    ),
    # Multi-translation-unit smoke test: tests/programs/multitu_demo.c
    # declares two functions that live in multitu_demo_helper.c (linked
    # in via the multitu_demo.deps sidecar).  cc.py --object emits
    # CCREL_CALL relocations for each cross-TU call; ccld resolves them
    # against the helper's globally-bound symbols.  A regression that
    # broke the linker's cross-object rel32 patching would either fail
    # the build or land at a non-3+4+35 sum here.
    ProgramTest("multitu_demo", ["multitu_demo"], r"^multitu_demo: 42$"),
    ProgramTest("mv", ["mkdir tmpe", "mv tmpe tmpf", "ls"], r"tmpf/"),
    # Writing to virt 0 raises #PF (PTE[0] is not-present in every
    # per-program PD, so page 0 is always unmapped).  The user-fault
    # kill path tears down the PD and respawns the shell; echo recovered
    # then runs to confirm the new shell works.
    ProgramTest("nullderef", ["fault_test null", "echo recovered"], r"EXC0E[\s\S]*CR2=00000000[\s\S]*recovered"),
    ProgramTest("okptest", ["fault_test kernel_buf", "echo recovered"], r"ok: bad pointer rejected[\s\S]*recovered"),
    ProgramTest("ping", ["ping 10.0.2.2"], r"(RTT=|time=|reply|timeout)", with_net=True, timeout=20.0),
    # play_midi opens /dev/midi, queues a 1 s A4 tone on OPL voice 0, and exits.
    # The QEMU SB16 device exposes the OPL3 synth at 0x388/0x38A so opl_probe
    # succeeds; without -device sb16 the open returns -1.  The audiodev=none
    # backend keeps the SB16 wired up without spawning a host audio sink.
    ProgramTest(
        "play_midi",
        ["audio_test midi"],
        r"^play_midi: done$",
        extra_qemu_args=["-audiodev", "none,id=a", "-device", "sb16,audiodev=a"],
        timeout=3.0,
    ),
    # recursive_exec_test calls exec("cat") from inside a running child.
    # The kernel rejects recursive exec because parent_program_state is
    # already set (the shell is the parent); sys_exec returns CF=1 with
    # AL = ERROR_INVALID (5), and the builtin wraps that as -(5) = -5.
    ProgramTest("recursive_exec_test", ["recursive_exec_test"], r"rc=-5"),
    ProgramTest(
        "recv_nonblock_test",
        ["recv_nonblock_test"],
        r"^OK$",
        with_net=True,
    ),
    ProgramTest(
        "recv_timeout_test",
        ["recv_timeout_test"],
        r"^OK$",
        with_net=True,
        timeout=15.0,
    ),
    ProgramTest(
        "rename",
        ["cp src/parse_ip.asm out.asm", "mv out.asm renamed.asm", "cat renamed.asm"],
        r"^parse_ip:",
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        "rename_cross_parent",
        ["mkdir sub", "cp src/parse_ip.asm sub/file.asm", "mv sub/file.asm out.asm", "cat out.asm"],
        r"^parse_ip:",
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        "rename_dir",
        ["mkdir mydir", "mv mydir newdir", "ls newdir"],
        r"^\.\./",
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        "rename_dir_cross_parent",
        ["mkdir sub", "mkdir mydir", "mv mydir sub/mydir", "ls sub/mydir"],
        r"^\.\./",
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        "rm",
        ["cp src/parse_ip.asm out.asm", "rm out.asm", "cat out.asm"],
        r"File not found",
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        "rmdir",
        ["mkdir mydir", "rmdir mydir", "ls mydir"],
        r"Not found",  # ls fails because mydir was successfully removed
        filesystems=_EXT2_ONLY,
    ),
    ProgramTest(
        "rmdir_nonempty",
        ["mkdir mydir", "cp src/parse_ip.asm mydir/file.asm", "rmdir mydir"],
        r"Not empty",
        filesystems=_EXT2_ONLY,
    ),
    # 1 KB recursive frames overflow the 16-page user stack into the
    # unmapped page below it; same kill path as nullderef.  CR2 lands
    # somewhere below STACK_VIRT_BASE (= USER_STACK_TOP - 0x10000) — match
    # the EXC0E signature loosely so future stack-size or KERNEL_VIRT_BASE
    # changes don't break this.
    # Exercises SYS_IO_SEEK end-to-end: SEEK_SET/CUR/END, EOF clamping,
    # and the read-cursor invariant.  Opens a known stable file
    # (src/macro_sm.asm, 1052 bytes) so the position-clamp assertion
    # stays meaningful across rebuilds.  Program name is 4 chars so
    # the new bin/ directory entry (rec_len 12) lands at offset 492 in
    # block 0, where the straddle_dir test still finds a usable
    # boundary at 512 — longer names push past 492 and break it.
    ProgramTest("seek", ["seek"], r"^seek: OK$"),
    ProgramTest("seq_basic", ["seq 3"], r"^1$\n^2$\n^3$"),
    ProgramTest("seq_range", ["seq 5 7"], r"^5$\n^6$\n^7$"),
    # [shell:start] is printed exactly once per shell-load at the top of
    # main().  Three successive echo commands must all run inside the same
    # shell instance — confirming shell-survives-child — so the marker
    # appears exactly once before all three outputs.  The negative
    # lookahead after the marker asserts it does not appear a second
    # time; a regression where the shell reloads between commands would
    # print [shell:start] again and trip the lookahead.
    ProgramTest(
        "shell_state_survives",
        ["echo a", "echo b", "echo c"],
        r"^\[shell:start\](?![\s\S]*\[shell:start\])[\s\S]*\ba\b[\s\S]*\bb\b[\s\S]*\bc\b",
    ),
    # Registers an on_sigint handler, calls SYS_IO_READ, and sends a
    # Ctrl+C (0x03) byte so fd_read_console detects it, sets
    # pending_sigint, and returns the byte.  The syscall epilogue's
    # SIGNAL_TAIL_CHECK dispatches to on_sigint via signal_dispatch_user;
    # the handler sets got_sigint and returns through the vDSO sigreturn
    # trampoline; signal_resume_after_handler restores the interrupted
    # state and iretds back to user code.  Main checks got_sigint and
    # prints CAUGHT, confirming the full delivery and sigreturn round-trip.
    # The "\n" in the command terminates the shell input line; the
    # following 0x03 byte arrives in the serial FIFO for the program's
    # read call to consume.
    ProgramTest("sigint_test", ["sigint_test\n\x03"], r"^CAUGHT$"),
    ProgramTest("sort_basic", ["echo -e banana\\napple\\ncherry | sort"], r"^apple\r?\nbanana\r?\ncherry\r?\n"),
    ProgramTest("sort_reverse", ["echo -e a\\nb\\nc | sort -r"], r"^c\r?\nb\r?\na\r?\n"),
    ProgramTest("sort_numeric", ["echo -e 10\\n2\\n1 | sort -n"], r"^1\r?\n2\r?\n10\r?\n"),
    ProgramTest("sort_unique", ["echo -e a\\na\\nb\\nb\\nb\\nc | sort -u"], r"^a\r?\nb\r?\nc\r?\n"),
    ProgramTest("stackbomb", ["stackbomb", "echo recovered"], r"stackbomb: starting recursion[\s\S]*EXC0E[\s\S]*recovered"),
    # Exercises cc.py's pointer-to-pointer support: strtol writes
    # *endptr (a char**) so the caller can recover the tail past the
    # parsed digits.  Verifies both the NULL classification fix and
    # the DerefAssign codegen for plain char** locals.
    ProgramTest("strtol_endptr", ["strtol_endptr"], r"^ENDPTR_OK value=42 tail=abc$"),
    # Confirms the user stack lives at the user/kernel boundary
    # (USER_STACK_TOP = KERNEL_VIRT_BASE).  ESP at iretd equals
    # USER_STACK_TOP, so the high byte is the high byte of
    # KERNEL_VIRT_BASE (currently 0xff at base 0xFF800000).
    ProgramTest("stacktop", ["stacktop"], r"^stacktop: high=FF$"),
    ProgramTest(
        # `_ext2_add_straddle_dir_filler` chains filler entries so
        # STRADDLE's 8-byte header ends exactly at a 512-byte sector
        # boundary, putting its name in the next sector.  Stronger
        # guarantee than multi_sector_dir, which only happens to
        # straddle for particular block_size + bin/ layouts.  Only
        # exercises 2 KB blocks: 1 KB blocks can't reach a usable
        # straddle boundary because bin/'s baseline entries already
        # extend past 504, leaving no room to stage a straddle there.
        "straddle_dir",
        commands=[],
        expect="",
        filesystems=_EXT2_ONLY,
        setup=_ext2_add_straddle_dir_filler,
    ),
    ProgramTest(
        "tail_file",
        ["seq 1 5 | tee seqfile", "tail -n 2 seqfile"],
        # Anchor on the tail command line so the tee output above can't
        # false-positive — the buggy version printed "2\n3" but the
        # upstream "1\n2\n3\n4\n5" from tee would still match a loose
        # "^4$\n^5$" regex.
        r"\$ tail -n 2 seqfile\r?\n4\r?\n5\r?\n\$",
    ),
    ProgramTest("tail_stdin", ["seq 1 5 | tail -n 2"], r"^4\r?\n5\r?\n\$"),
    # ~589 KB of input through the 64 KB ring forces eviction many times.
    # If the ring eviction logic is wrong, the final 3 lines won't match.
    ProgramTest(
        "tail_stdin_overflow",
        ["seq 1 100000 | tail -n 3"],
        r"^99998\r?\n99999\r?\n100000\r?\n\$",
        timeout=5.0,
    ),
    ProgramTest("tail_stdin_zero", ["seq 1 5 | tail -n 0; echo done"], r"^done\r?\n\$"),
    ProgramTest(
        "tee_basic",
        ["echo hello | tee teefile", "cat teefile"],
        r"hello[\s\S]*hello",
    ),
    ProgramTest("tr_basic", ["echo HELLO | tr A-Z a-z"], r"^hello\r?\n\$"),
    ProgramTest("tr_delete", ["echo abc123 | tr -d 0-9"], r"^abc\r?\n\$"),
    ProgramTest("tr_delete_extra_arg", ["tr -d abc xyz; echo $?"], r"Usage: tr -d <set1>"),
    ProgramTest("tr_literal", ["echo abc | tr a x"], r"^xbc\r?\n\$"),
    ProgramTest("tr_mismatch", ["echo abc | tr abc xy; echo $?"], r"tr: set length mismatch"),
    ProgramTest("true_chain", ["true && echo ran || echo skipped"], r"^ran$"),
    ProgramTest("uniq_basic", ["echo -e a\\na\\nb\\nb\\na | uniq"], r"^a\r?\nb\r?\na\r?\n\$"),
    ProgramTest("uniq_blank_lines", ["echo -n -e \\n\\n\\n | uniq -c"], r"^3 \r?\n\$"),
    ProgramTest("uniq_count", ["echo -e a\\na\\nb | uniq -c"], r"^2 a\r?\n1 b\r?\n\$"),
    ProgramTest("uniq_dups", ["echo -e a\\na\\nb\\nb\\nc | uniq -d"], r"^a\r?\nb\r?\n\$"),
    ProgramTest("uniq_unterminated_last", ["echo -n -e foo\\nfoo | uniq -c"], r"^2 foo\r?\n\$"),
    ProgramTest("uptime", ["uptime"], r"\d+:\d{2}:\d{2}"),
    ProgramTest("wc_lines", ["seq 1 7 | wc -l"], r"^7\s*$"),
    ProgramTest("wc_default", ["seq 1 2 | wc"], r"^2\s+2\s+4\s*$"),
    ProgramTest("yes_head", ["yes hi | head -n 3"], r"^hi$\n^hi$\n^hi$"),
]


# Subset of ext2 tests re-run with 2 KB blocks (exercises variable-block-size
# paths).  Only ext2 tests that actually touch the filesystem are included —
# CPU/network/cc.py tests don't change behavior with block size.  cat_large
# and the doubly_indirect_* trio are 1 KB-only because src/large.bin is
# injected exclusively into the 1 KB image (the doubly-indirect threshold
# is 268 KB at 1 KB blocks; the same data layout doesn't reach the doubly-
# indirect region at 2 KB blocks, where the threshold is 1 MB).
_EXT2_BLOCK_SIZE_2K_TEST_NAMES = frozenset({
    "cat",
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
    "straddle_dir",  # 2 KB-only test; needs the larger block to stage a straddle
})


# ---------------------------------------------------------------------------
# Build, run, fsck
# ---------------------------------------------------------------------------


def _build_os(*, block_size: int, filesystem: str, large_file: bool, temporary_directory: Path) -> None:
    """Run make_os.sh with --with-test-programs and the right FS flags."""
    image = temporary_directory / BASE_IMAGE
    command = ["./make_os.sh", "--with-test-programs"]
    if filesystem == "ext2":
        command += ["--ext2", f"--ext2-block-size={block_size}", "--ext2-inode-count=1024"]
    command.append(str(image))
    result = subprocess.run(command, capture_output=True, check=False, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(1)
    if filesystem == "ext2" and large_file and block_size == 1024:
        _ext2_add_large_test_file(image=image)


def _run_test(*, filesystem: str, floppy: bool, temporary_directory: Path, test: ProgramTest) -> tuple[bool, str, float, float]:
    """Run one ProgramTest; return (passed, message, boot_time, command_time).

    ext2 tests always copy the image (so e2fsck can inspect post-test state)
    and run with snapshot=False; bbfs tests reuse the base image with
    snapshot=True unless the test has a setup hook that mutates it.
    """
    if filesystem == "ext2" or test.setup is not None:
        drive = temporary_directory / f"test_{test.name}.img"
        shutil.copy2(temporary_directory / BASE_IMAGE, drive)
        if test.setup is not None:
            test.setup(image=drive, test=test)
        snapshot = False
    else:
        drive = temporary_directory / BASE_IMAGE
        snapshot = True
    try:
        result = run_commands(
            test.commands,
            command_timeout=test.timeout,
            drive=drive,
            extra_qemu_args=test.extra_qemu_args or None,
            floppy=floppy,
            memory=test.memory,
            snapshot=snapshot,
            with_net=test.with_net,
        )
    except TimeoutError as error:
        return False, f"timeout: {error}", 0.0, 0.0
    except RuntimeError as error:
        return False, f"qemu error: {error}", 0.0, 0.0
    command_time = sum(result.command_times)
    failures: list[str] = []
    if not re.search(test.expect, result.output.replace("\r", ""), re.MULTILINE):
        failures.append(f"expected regex {test.expect!r} not found in output")
    if filesystem == "ext2":
        fsck_error = _ext2_fsck(image=drive)
        if fsck_error:
            failures.append(f"fsck: {fsck_error}")
    return (not failures), "; ".join(failures), result.boot_time, command_time


def _run_suite(
    *,
    fail_fast: bool,
    filesystem: str,
    floppy: bool,
    label: str,
    tests: list[ProgramTest],
    temporary_directory: Path,
) -> tuple[int, int, list[str]]:
    """Run a list of ProgramTests; return (pass_count, fail_count, failed_names)."""
    pass_count = 0
    fail_count = 0
    failed: list[str] = []
    for test in tests:
        display_name = f"{label}{test.name}" if label else test.name
        if test.skip is not None:
            print(f"  SKIP  {display_name:<24} ({test.skip})")
            continue
        ok, message, boot_time, command_time = _run_test(
            filesystem=filesystem,
            floppy=floppy,
            temporary_directory=temporary_directory,
            test=test,
        )
        timing = f"boot {boot_time:.2f}s  cmd {command_time:.2f}s"
        if ok:
            print(f"  PASS  {display_name:<24}              {timing}")
            pass_count += 1
        else:
            print(f"  FAIL  {display_name:<24}  {message}   {timing}")
            fail_count += 1
            failed.append(display_name)
            if fail_fast:
                break
    return pass_count, fail_count, failed


def main() -> int:
    """Run the selected ProgramTests and print a summary."""
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="restrict to one program (e.g. 'arp')")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first failing test",
    )
    parser.add_argument(
        "--filesystem",
        choices=("bbfs", "ext2"),
        default="bbfs",
        help="select the filesystem build under test (default: bbfs)",
    )
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="boot QEMU with the drive attached as a floppy (if=floppy); on ext2 "
        "this skips the 2 KB-block matrix because the resulting image exceeds "
        "the 1.44 MB floppy capacity",
    )
    parser.add_argument(
        "--slow",
        action="store_true",
        help="include slow tests (bigbss tripwire on either filesystem; ext2 large-file and doubly-indirect on --filesystem ext2)",
    )
    arguments = parser.parse_args()

    tests = [t for t in TESTS if arguments.filesystem in t.filesystems and (arguments.program is None or t.name == arguments.program)]
    if not tests:
        if arguments.program is None:
            print(f"No tests for filesystem {arguments.filesystem!r}")
        else:
            print(f"No test named {arguments.program!r} for filesystem {arguments.filesystem!r}")
        return 1

    if arguments.program is None and not arguments.slow:
        for test in tests:
            if test.slow:
                print(f"  SKIP  {test.name:<24} (slow; pass --slow to include)")
        tests = [t for t in tests if not t.slow]

    total_pass = 0
    total_fail = 0
    all_failed: list[str] = []

    with tempfile.TemporaryDirectory(prefix=f"test_programs_{arguments.filesystem}_") as temporary_path:
        temporary_directory = Path(temporary_path)
        _build_os(
            block_size=1024,
            filesystem=arguments.filesystem,
            large_file=arguments.slow,
            temporary_directory=temporary_directory,
        )
        passed, failed_count, failed_names = _run_suite(
            fail_fast=arguments.fail_fast,
            filesystem=arguments.filesystem,
            floppy=arguments.floppy,
            label="",
            tests=tests,
            temporary_directory=temporary_directory,
        )
        total_pass += passed
        total_fail += failed_count
        all_failed += failed_names

    # 2 KB block-size matrix (ext2 only; full-suite only; not under --floppy:
    # mke2fs grows a 2 KB-block image past 1.44 MB so it can't be addressed
    # via QEMU's floppy backend).
    run_2k_matrix = (
        arguments.filesystem == "ext2" and arguments.program is None and not arguments.floppy and not (arguments.fail_fast and total_fail)
    )
    if run_2k_matrix:
        block_2k_tests = [t for t in TESTS if t.name in _EXT2_BLOCK_SIZE_2K_TEST_NAMES and "ext2" in t.filesystems]
        if not arguments.slow:
            block_2k_tests = [t for t in block_2k_tests if not t.slow]
        with tempfile.TemporaryDirectory(prefix="test_programs_ext2_2k_") as temporary_path:
            temporary_directory = Path(temporary_path)
            _build_os(
                block_size=2048,
                filesystem="ext2",
                large_file=False,
                temporary_directory=temporary_directory,
            )
            passed, failed_count, failed_names = _run_suite(
                fail_fast=arguments.fail_fast,
                filesystem="ext2",
                floppy=arguments.floppy,
                label="2k/",
                tests=block_2k_tests,
                temporary_directory=temporary_directory,
            )
            total_pass += passed
            total_fail += failed_count
            all_failed += failed_names
    elif arguments.filesystem == "ext2" and arguments.program is None and arguments.floppy:
        block_2k_count = sum(1 for t in TESTS if t.name in _EXT2_BLOCK_SIZE_2K_TEST_NAMES and "ext2" in t.filesystems)
        print(f"  SKIP  2k/* ({block_2k_count} tests) — image exceeds 1.44 MB floppy capacity")

    print()
    print(f"{total_pass} passed, {total_fail} failed")
    if total_fail:
        print("Failed:", " ".join(all_failed))
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
