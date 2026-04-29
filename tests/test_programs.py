#!/usr/bin/env python3
"""Runtime smoke tests for user-space programs.

For each program in `bin/`, boot bboeos in QEMU, run a representative
command, and check the output against an expected regex. Each test
gets its own QEMU boot with `snapshot=on`, so writes don't pollute
drive.img.

Skips `shell` (implicit) and `asm` (covered by test_asm.py).
Skips `draw` and `edit` (interactive; no deterministic output).

Usage:
    ./test_programs.py            # run the full suite
    ./test_programs.py netinit    # run one program
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from run_qemu import run_commands

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_IMAGE = "drive.img"
_DEFAULT_PROGRAM_TIMEOUT = float(os.environ.get("BBOE_PROGRAM_TIMEOUT", "1.0"))


@dataclass
class ProgramTest:
    """One runtime test: shell commands to run and a regex the output must match."""

    name: str
    commands: list[str]
    expect: str
    with_net: bool = False
    timeout: float = _DEFAULT_PROGRAM_TIMEOUT
    skip: str | None = None


TESTS: list[ProgramTest] = [
    ProgramTest("arp", ["arp 10.0.2.2"], r"10\.0\.2\.2 is at [0-9A-F:]+", with_net=True),
    ProgramTest("asmesc", ["asmesc"], r"^value = 7$"),
    ProgramTest("bits", ["bits"], r"^b-=  = 46$"),
    ProgramTest("booltest", ["booltest"], r"^sum      = 3$"),
    ProgramTest("cat", ["cat src/parse_ip.asm"], r"^parse_ip:"),
    ProgramTest("cftest", ["cftest"], r"tick\(\) fired 3 times, remaining = 0"),
    ProgramTest("chmod", ["chmod +x hello"], r"\$"),
    ProgramTest("cp", ["cp src/parse_ip.asm tmpb", "ls"], r"tmpb"),
    ProgramTest("date", ["date"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"),
    ProgramTest("dns", ["dns example.com"], r"example\.com is at \d+\.\d+\.\d+\.\d+", with_net=True, timeout=30.0),
    ProgramTest("echo", ["echo foo bar baz"], r"^foo bar baz$"),
    ProgramTest("echo_many_args", ["echo a b c d e", "ls"], r"^a b c d e$"),
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
    ProgramTest("okptest", ["okptest", "echo recovered"], r"ok: bad pointer rejected[\s\S]*recovered"),
    ProgramTest("pintest", ["pintest"], r"^first non-space: h$"),
    ProgramTest("ping", ["ping 10.0.2.2"], r"(RTT=|time=|reply|timeout)", with_net=True, timeout=20.0),
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
    try:
        result = run_commands(
            test.commands,
            command_timeout=test.timeout,
            drive=temporary_directory / BASE_IMAGE,
            floppy=floppy,
            snapshot=True,
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
