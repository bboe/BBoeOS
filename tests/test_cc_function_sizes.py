#!/usr/bin/env python3
"""Per-function byte-size differential gate for the cc.py Place refactor.

Compiles every userland C translation unit with cc.py's object-file +
per-function-sections pipeline, assembles each to an ELF object with nasm,
and reads the byte size of every ``.text.<function>`` section via readelf.
The result is compared against a committed baseline.

Gate policy (the Plan 5 byte-EFFICIENCY rule):
  * A function whose size GREW versus the baseline FAILS the test — a
    refactor must never make any function larger without an explicit,
    justified baseline refresh.
  * A function that SHRANK is reported but does not fail (size wins are
    welcome); refresh the baseline to capture the improvement.
  * A new or removed function is reported and fails until the baseline is
    refreshed (so additions/removals are always a deliberate baseline edit).

Refresh the baseline deliberately with BBOE_UPDATE_SIZES=1 only when a size
change is intended (a justified perf-driven increase, an accepted decrease,
or an added/removed function).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "tests" / "golden" / "cc_function_sizes_baseline.json"
CC = REPO_ROOT / "cc.py"
KERNEL_INCLUDE = REPO_ROOT / "kernel" / "include"
# Userland translation units cc.py compiles through the object pipeline.
# Kernel .c is excluded (compiled with --target kernel, a different path);
# this gate covers the user / libbboeos surface the Place family touches.
SOURCE_GLOBS = ("user/libbboeos/*.c", "user/programs/*.c")

SYSCALLS_HEADER_GENERATOR = REPO_ROOT / "tools" / "generate_syscalls_h.py"


def compare_source(
    *,
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
    failures: list[str],
    improvements: list[str],
    source: str,
) -> None:
    """Compare one translation unit against its baseline entry."""
    functions = current[source]
    baseline_functions = baseline.get(source)
    if baseline_functions is None:
        failures.append(f"{source}: new translation unit (refresh baseline)")
        return
    for name, size in functions.items():
        if name not in baseline_functions:
            failures.append(f"{source}:{name}: new function (refresh baseline)")
        elif size > baseline_functions[name]:
            failures.append(f"{source}:{name}: {baseline_functions[name]} -> {size} bytes (GREW)")
        elif size < baseline_functions[name]:
            improvements.append(f"{source}:{name}: {baseline_functions[name]} -> {size} bytes (shrank)")
    failures.extend(f"{source}:{name}: function removed (refresh baseline)" for name in baseline_functions if name not in functions)


def discover_sources() -> list[Path]:
    """Return every userland .c path (sorted, repo-relative-stable)."""
    sources: list[Path] = []
    for glob in SOURCE_GLOBS:
        directory, pattern = glob.rsplit("/", 1)
        sources.extend(sorted((REPO_ROOT / directory).glob(pattern)))
    return sources


def function_sizes(*, source: Path, work: Path) -> dict[str, int]:
    """Compile *source* and return ``{function_name: byte_size}``."""
    asm_path = work / (source.stem + ".asm")
    object_path = work / (source.stem + ".o")
    subprocess.run(
        [sys.executable, str(CC), "--bits", "32", "--object", "--per-function-sections", str(source), str(asm_path)],
        check=True,
    )
    subprocess.run(
        ["nasm", "-f", "elf32", "-i", str(KERNEL_INCLUDE) + "/", str(asm_path), "-o", str(object_path)],
        check=True,
    )
    readelf = subprocess.run(
        ["readelf", "-SW", str(object_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    sizes: dict[str, int] = {}
    section_prefix = ".text."
    for line in readelf.stdout.splitlines():
        # readelf -SW section-header row format:
        #   "[ N] .text.<name> PROGBITS <addr> <off> <size> ..."  # ruff:ignore[commented-out-code] — illustrative format string, not commented-out code
        stripped = line.strip()
        if section_prefix not in stripped or "PROGBITS" not in stripped:
            continue
        # Fields after the closing bracket: name, type, addr, off, size, ...
        after_bracket = stripped.split("]", 1)[-1].split()
        if len(after_bracket) < 5 or not after_bracket[0].startswith(section_prefix):
            continue
        name = after_bracket[0][len(section_prefix) :]
        sizes[name] = int(after_bracket[4], 16)
    return sizes


def generate_syscalls_header() -> None:
    """Generate the build-time ``include/syscalls.h`` the libbboeos sources need.

    ``user/libbboeos/include/syscalls.h`` is generated from
    ``kernel/include/constants.asm`` by ``tools/generate_syscalls_h.py``
    (run by ``make_os.sh`` before it compiles the libbboeos C sources) and
    is gitignored.  A fresh checkout — CI, or a tree that has never built —
    lacks it, so ``signal.c`` and friends fail to resolve the include.
    Regenerate it here so the gate is self-contained.
    """
    subprocess.run([sys.executable, str(SYSCALLS_HEADER_GENERATOR)], check=True)


def main() -> int:
    """Run the gate, or refresh the baseline when BBOE_UPDATE_SIZES=1."""
    current = measure_all()
    if os.environ.get("BBOE_UPDATE_SIZES") == "1":
        BASELINE.parent.mkdir(exist_ok=True, parents=True)
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"WROTE baseline {BASELINE}")
        return 0
    baseline = json.loads(BASELINE.read_text())
    failures: list[str] = []
    improvements: list[str] = []
    for source in current:
        compare_source(
            baseline=baseline,
            current=current,
            failures=failures,
            improvements=improvements,
            source=source,
        )
    failures.extend(f"{source}: translation unit removed (refresh baseline)" for source in baseline if source not in current)
    for note in improvements:
        print(f"IMPROVED {note}")
    if failures:
        print("FAIL  per-function byte-size gate")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"PASS  per-function byte-size gate ({sum(len(f) for f in current.values())} functions, {len(current)} files)")
    return 0


def measure_all() -> dict[str, dict[str, int]]:
    """Return ``{repo_relative_source: {function: size}}`` for every source."""
    generate_syscalls_header()
    result: dict[str, dict[str, int]] = {}
    with tempfile.TemporaryDirectory(prefix="cc_func_sizes_") as temporary:
        work = Path(temporary)
        for source in discover_sources():
            relative = str(source.relative_to(REPO_ROOT))
            result[relative] = function_sizes(source=source, work=work)
    return result


if __name__ == "__main__":
    sys.exit(main())
