#!/usr/bin/env python3
"""Runtime smoke tests for user-space programs.

For each program in `bin/`, boot bboeos in QEMU, run a representative
command, and check the output against an expected regex. Each test
gets its own QEMU boot with `snapshot=on`, so writes don't pollute
drive.img.

Skips `shell` (implicit) and `asm` (covered by test_asm.py).

Usage:
    ./test_programs.py            # run the full suite
    ./test_programs.py netinit    # run one program
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
BASE_IMAGE = "drive.img"
_DEFAULT_PROGRAM_TIMEOUT = float(os.environ.get("BBOE_PROGRAM_TIMEOUT", "1.0"))

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
    find_subdirectory_entry,
    iter_entries,
)

_BBFS_DIRECTORY_SECTORS = 3
_BBFS_DIRECTORY_MAX_ENTRIES = _BBFS_DIRECTORY_SECTORS * ENTRIES_PER_SECTOR  # 48


@dataclass
class ProgramTest:
    """One runtime test: shell commands to run and a regex the output must match.

    `memory` overrides `run_commands`'s 1 MB default for tests whose
    program needs more.  No program in `bin/` currently does — every
    user image fits comfortably in `-m 1` after `EDIT_BUFFER_SIZE`
    dropped to 0x70000 (448 KB).  Kept on the dataclass as an escape
    hatch for future heavier programs.
    """

    name: str
    commands: list[str]
    expect: str
    setup: Callable[[Path, ProgramTest], None] | None = None
    with_net: bool = False
    timeout: float = _DEFAULT_PROGRAM_TIMEOUT
    skip: str | None = None
    memory: str | None = None


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


def _bin_entry_names(*, image: Path) -> list[str | None]:
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


def _pad_bin_to_full_directory(image: Path, test: ProgramTest) -> None:
    """Pad bin/ to BBfs's 48-entry cap with an executable probe written last.

    bbfs subdirectories don't carry . / ..; bin/ starts populated with
    the PROGRAMS list (count varies as PROGRAMS grows).  The setup
    counts the existing entries, adds (47 - existing) empty fillers
    in a single batched image flush, then writes _zexec_last as the
    literal final entry (slot 47, in sector 2 of bbfs's 3-sector
    directory).  Asserts arp (slot 0, sector 0), a runtime-picked
    sector-1 entry (slots 16..31, name chosen from the post-padding
    bin/ layout so the test stays robust to PROGRAMS reordering), and
    _zexec_last (slot 47, sector 2) all resolve so the lookup walks
    all three of bbfs's directory sectors.
    """
    names = _bin_entry_names(image=image)
    used = sum(1 for name in names if name is not None)
    fillers_needed = _BBFS_DIRECTORY_MAX_ENTRIES - used - 1
    if fillers_needed < 0:
        msg = f"bin/ already at or past cap ({used}/{_BBFS_DIRECTORY_MAX_ENTRIES}); cannot place _zexec_last"
        raise RuntimeError(msg)
    add_empty_files(
        image_path=str(image),
        names=[f"_pad{filler_index:02d}" for filler_index in range(fillers_needed)],
        subdirectory="bin",
    )
    _add_exec_probe(image=image, name="_zexec_last")

    middle_name, middle_expect = _pick_sector1_probe(names=_bin_entry_names(image=image))
    test.commands = ["arp", middle_name, "_zexec_last"]
    test.expect = (
        r"usage: arp <ip>"
        rf"[\s\S]+{middle_expect}"
        r"[\s\S]+^EXEC _zexec_last$"
    )


def _pick_sector1_probe(*, names: list[str | None]) -> tuple[str, str]:
    """Return (program_name, expected_regex) for some entry in sector 1.

    Sector 1 spans slots 16..31.  Walks those slots in order and picks
    the first whose program has a single-command, non-network entry in
    TESTS so its expected output regex is reusable here.  Robust to
    PROGRAMS growth: as long as some sector-1 slot still maps to a
    self-contained TEST, the lookup keeps exercising all three bbfs
    directory sectors during the `_zexec_last` walk and the picked
    program separately confirms a sector-1 entry resolves.
    """
    runnable = {
        test.name: test.expect
        for test in TESTS
        if test.commands == [test.name] and not test.with_net and test.setup is None and test.skip is None
    }
    sector_1_start = ENTRIES_PER_SECTOR
    sector_1_end = 2 * ENTRIES_PER_SECTOR
    for slot in range(sector_1_start, sector_1_end):
        name = names[slot]
        if name is not None and name in runnable:
            return name, runnable[name]
    msg = f"no testable program in bin/'s sector 1 (slots {sector_1_start}..{sector_1_end - 1}); update TESTS or _pick_sector1_probe"
    raise RuntimeError(msg)


TESTS: list[ProgramTest] = [
    ProgramTest("arp", ["arp 10.0.2.2"], r"10\.0\.2\.2 is at [0-9A-F:]+", with_net=True),
    ProgramTest("asmesc", ["asmesc"], r"^value = 7$"),
    # Maximum-BSS success case AND kmap-window smoke test.  bigbss
    # declares BIGBSS_PAGES (see src/c/bigbss_size.h) = 523,341 of
    # BSS at -m 2048 — large enough that ~half the frames sit
    # above FRAME_DIRECT_MAP_LIMIT (~1020 MB).  program_enter's
    # phase-2 zero-fills those high frames through the kmap window
    # (memory_management/kmap.asm), so a successful run validates
    # kmap_map / kmap_unmap end-to-end.  The verify pass after the
    # write loop catches any kmap zero-fill that lands at the wrong
    # phys.
    ProgramTest("bigbss", ["bigbss"], r"^bigbss: OK$", memory="2048", timeout=180.0),
    # Tripwire-low: same program at -m 2047 (one MB less RAM, ~256
    # fewer frames in the bitmap).  At -m 2047 BIGBSS_PAGES + per-PD
    # overhead no longer fits, and program_enter OOMs partway
    # through phase 2 (also exercising address_space_destroy on a
    # partially-built PD whose user PTs landed both below and above
    # the direct-map ceiling).  Asserts the OOM message AND a
    # follow-up `hello` runs in the respawned shell.  If
    # BIGBSS_PAGES drifts down by more than ~256 frames, this test
    # starts fitting and we lose the lower tripwire.
    ProgramTest(
        "bigbss_oom",
        ["bigbss", "hello"],
        r"^exec: out of memory$[\s\S]+^Hello world!$",
        memory="2047",
        timeout=120.0,
    ),
    # Tripwire-high: bigbss_fail declares BIGBSS_PAGES + 1 of BSS —
    # exactly one page beyond what bigbss fits at -m 2048 — and
    # asserts OOM.  Page-precise: any upward drift in BIGBSS_PAGES
    # makes this fit and the test fails (no OOM message).  Together
    # bigbss_oom + bigbss_fail pin BIGBSS_PAGES to the page-precise
    # ceiling at -m 2048.
    ProgramTest(
        "bigbss_fail",
        ["bigbss_fail", "hello"],
        r"^exec: out of memory$[\s\S]+^Hello world!$",
        memory="2048",
        timeout=60.0,
    ),
    ProgramTest("bits", ["bits"], r"^b-=  = 46$"),
    ProgramTest("booltest", ["booltest"], r"^sum      = 3$"),
    ProgramTest("cat", ["cat src/parse_ip.asm"], r"^parse_ip:"),
    ProgramTest("cftest", ["cftest"], r"tick\(\) fired 3 times, remaining = 0"),
    ProgramTest("chmod", ["chmod +x hello"], r"\$"),
    ProgramTest("cp", ["cp src/parse_ip.asm tmpb", "ls"], r"tmpb"),
    ProgramTest("date", ["date"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"),
    ProgramTest("dns", ["dns example.com"], r"example\.com is at \d+\.\d+\.\d+\.\d+", with_net=True, timeout=30.0),
    # 'draw\nq' runs `draw`, then draw reads the trailing 'q' from the
    # serial buffer and exits its main loop (back to text mode).  draw
    # has no serial output of its own — all writes go to VGA — so the
    # follow-up `hello` is what the regex matches: if draw crashed or
    # left the shell wedged in graphics mode, hello would never run.
    # See the `edit` entry below for the same pattern with Ctrl+Q.
    ProgramTest("draw", ["draw\nq", "hello"], r"^\$ draw[\s\S]+Hello world!"),
    ProgramTest("echo", ["echo foo bar baz"], r"^foo bar baz$"),
    ProgramTest("echo_many_args", ["echo a b c d e", "ls"], r"^a b c d e$"),
    # 'edit hello\n\x11' runs `edit hello`, then edit consumes the trailing
    # Ctrl+Q (\x11) from the serial buffer.  hello doesn't exist in cwd, so
    # edit opens with an empty buffer; with dirty=0 a single Ctrl+Q exits.
    # The follow-up `hello` command confirms the shell is fully functional
    # again — catches PD teardown / VGA mode reset bugs that would
    # otherwise leave the shell wedged.  Doubles as a regression for the
    # 448 KB BSS allocation in the per-program PD.  Relies on
    # _wait_for_prompt's settle window to drain the spurious empty-line
    # prompt (from the trailing '\r' shell consumes after edit exits)
    # before this command's wait begins.
    ProgramTest("edit", ["edit hello\n\x11", "hello"], r"^hello  line 1  col 1[\s\S]+Hello world!"),
    ProgramTest(
        # Pad bin/ with empty fillers until BBfs's 48-entry cap is hit,
        # ending with a single executable probe so the final directory
        # entry is something we can exec.  Asserts arp (first file
        # entry), loop_array (a program in the middle of bin/), and
        # _zexec_last (the literal last entry) all resolve.  The setup
        # writes the test's commands+expect post-padding.
        "exec_first_middle_last",
        commands=[],
        expect="",
        setup=_pad_bin_to_full_directory,
    ),
    ProgramTest("fctest", ["fctest"], r"accumulate\(9\)    = 28"),
    ProgramTest("gdemo", ["gdemo"], r"glob\[4\] = 15"),
    ProgramTest("gptest", ["gptest", "echo recovered"], r"EXC0D[\s\S]*recovered"),
    ProgramTest("gtable", ["gtable"], r"fib\[9\] = 55"),
    ProgramTest("hello", ["hello"], r"Hello world!"),
    ProgramTest("inctest", ["inctest"], r"^square = 144$"),
    ProgramTest("loop", ["loop"], r"aaaaa"),
    ProgramTest("loop_array", ["loop_array"], r"abc"),
    ProgramTest("ls", ["ls bin"], r"hello\*"),
    ProgramTest("mkdir", ["mkdir tmpd", "ls"], r"tmpd/"),
    ProgramTest("mv", ["mkdir tmpe", "mv tmpe tmpf", "ls"], r"tmpf/"),
    ProgramTest("netinit", ["netinit"], r"NIC found: [0-9A-F:]+", with_net=True),
    ProgramTest("netrecv", ["netrecv"], r"Received:.*08 06", with_net=True, timeout=20.0),
    ProgramTest("netsend", ["netsend"], r"ARP request sent", with_net=True),
    # Writing to virt 0 raises #PF (PTE[0] is not-present in every
    # per-program PD; the shell↔program handoff frame moved to
    # USER_DATA_BASE = 0x1000 to keep page 0 unmapped).  The user-fault
    # kill path tears down the PD and respawns the shell; echo recovered
    # then runs to confirm the new shell works.
    ProgramTest("nullderef", ["nullderef", "echo recovered"], r"EXC0E[\s\S]*CR2=00000000[\s\S]*recovered"),
    ProgramTest("okptest", ["okptest", "echo recovered"], r"ok: bad pointer rejected[\s\S]*recovered"),
    ProgramTest("pintest", ["pintest"], r"^first non-space: h$"),
    ProgramTest("ping", ["ping 10.0.2.2"], r"(RTT=|time=|reply|timeout)", with_net=True, timeout=20.0),
    # 1 KB recursive frames overflow the 16-page user stack into the
    # unmapped page below it; same kill path as nullderef.  CR2 lands
    # somewhere below 0xBFFF0000 (the stack base) — match the EXC0E
    # signature loosely so future stack-size changes don't break this.
    ProgramTest("stackbomb", ["stackbomb", "echo recovered"], r"stackbomb: starting recursion[\s\S]*EXC0E[\s\S]*recovered"),
    # Confirms the user stack lives at the new top (USER_STACK_TOP =
    # 0xC0000000, sitting at the user/kernel boundary).  ESP at iretd
    # equals USER_STACK_TOP, so the high byte is 0xC0 — any other
    # value (e.g. 0x40 from the pre-lift layout) means the lift
    # didn't take effect.
    ProgramTest("stacktop", ["stacktop"], r"^stacktop: high=C0$"),
    ProgramTest("uptime", ["uptime"], r"\d+:\d{2}:\d{2}"),
]


def _build_os(*, temporary_directory: Path) -> None:
    """Run make_os.sh; abort if the build fails."""
    image = temporary_directory / BASE_IMAGE
    result = subprocess.run(["./make_os.sh", str(image)], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(1)


def _run_test(*, floppy: bool, temporary_directory: Path, test: ProgramTest) -> tuple[bool, str, float, float]:
    """Run one ProgramTest; return (passed, message, boot_time, command_time)."""
    if test.setup is None:
        drive = temporary_directory / BASE_IMAGE
        snapshot = True
    else:
        drive = temporary_directory / f"test_{test.name}.img"
        shutil.copy2(temporary_directory / BASE_IMAGE, drive)
        test.setup(drive, test)
        snapshot = False
    try:
        result = run_commands(
            test.commands,
            command_timeout=test.timeout,
            drive=drive,
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
    if re.search(test.expect, result.output.replace("\r", ""), re.MULTILINE):
        return True, "", result.boot_time, command_time
    return False, f"expected regex {test.expect!r} not found in output", result.boot_time, command_time


def main() -> int:
    """Run the selected ProgramTests and print a summary."""
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("program", nargs="?", help="restrict to one program (e.g. 'netinit')")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop after the first failing test",
    )
    parser.add_argument(
        "--floppy",
        action="store_true",
        help="boot QEMU with the drive attached as a floppy (if=floppy)",
    )
    arguments = parser.parse_args()

    tests = [t for t in TESTS if arguments.program is None or t.name == arguments.program]
    if not tests:
        print(f"No test named {arguments.program!r}")
        return 1

    with tempfile.TemporaryDirectory(prefix="test_programs_") as temporary_path:
        temporary_directory = Path(temporary_path)
        _build_os(temporary_directory=temporary_directory)

        pass_count = 0
        fail_count = 0
        failed: list[str] = []
        for test in tests:
            if test.skip is not None:
                print(f"  SKIP  {test.name:<12} ({test.skip})")
                continue
            ok, message, boot_time, command_time = _run_test(
                floppy=arguments.floppy,
                temporary_directory=temporary_directory,
                test=test,
            )
            timing = f"boot {boot_time:.2f}s  cmd {command_time:.2f}s"
            if ok:
                print(f"  PASS  {test.name:<12}              {timing}")
                pass_count += 1
            else:
                print(f"  FAIL  {test.name:<12}  {message}   {timing}")
                fail_count += 1
                failed.append(test.name)
                if arguments.fail_fast:
                    break
    print()
    print(f"{pass_count} passed, {fail_count} failed")
    if fail_count:
        print("Failed:", " ".join(failed))
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
