#!/usr/bin/env python3
"""Recalibrate BIGBSS_PAGES for the page-precise tripwire trio.

Opt-in tool — run when a kernel-layout change makes the tripwire
tests (`bigbss`, `bigbss_oom`, `bigbss_fail`) go red.  The committed
constant in `tests/programs/bigbss_size.h` is the source of truth for
normal builds; this tool only edits that file when asked.

When to use it
--------------
Prefer the manual fix for small, known drifts: the failing test names
the direction (`bigbss_oom` → bump down, `bigbss_fail` → bump up),
and a one-line edit + single `tests/test_programs.py` rerun is ~75 s.
A full bracket+bisect here is 5-10 probes (6-12 min), so this tool
only wins when the drift direction or magnitude is unclear, or when
you want an unattended run (kick it off, walk away).

What it does
------------
1. Binary-search upward from the current `BIGBSS_PAGES` value to find
   the largest N where the `bigbss` program completes at -m 2048.
2. Write `BIGBSS_PAGES = N` back to `tests/programs/bigbss_size.h`.
3. Optionally verify the full tripwire trio (`--verify`):
       * bigbss      @ -m 2048   must pass
       * bigbss_fail @ -m 2048   must OOM (N + 1 doesn't fit)
       * bigbss_oom  @ -m 2047   must OOM

Each probe is a full `./make_os.sh` + QEMU boot, ~30-60 s.  A typical
recalibration after a small layout shift is 3-6 probes.

Usage
-----
    ./tools/calibrate_bigbss.py                # search + write
    ./tools/calibrate_bigbss.py --dry-run      # search, don't write
    ./tools/calibrate_bigbss.py --verify       # also run the trio
    ./tools/calibrate_bigbss.py --start 523000 # override starting point
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HEADER_PATH = REPO_ROOT / "tests" / "programs" / "bigbss_size.h"
DEFINE_PATTERN = re.compile(r"^(#define BIGBSS_PAGES )(\d+)[ \t]*(?=\r?\n|$)", re.MULTILINE)


def _bracket(*, start: int) -> tuple[int, int]:
    """Find (low, high) where bigbss passes at low and fails at high."""
    if _probe(pages=start):
        low, high, step = start, start, 1
        while _probe(pages=high + step):
            low = high + step
            step *= 2
            high = low
        return low, low + step
    high, low, step = start, start, 1
    while not _probe(pages=low - step):
        high = low - step
        step *= 2
        low = high
    return high - step, high


def _bisect(*, high: int, low: int) -> int:
    """Binary-search for largest passing N in (low, high)."""
    while high - low > 1:
        middle = (low + high) // 2
        if _probe(pages=middle):
            low = middle
        else:
            high = middle
    return low


def _verify(*, pages: int) -> int:
    """Run the full tripwire trio.  Return 0 on success, 2 on failure."""
    _set_pages(pages=pages)
    for name, requirement in [("bigbss", "pass @ -m 2048"), ("bigbss_fail", "OOM @ -m 2048"), ("bigbss_oom", "OOM @ -m 2047")]:
        print(f"verify: {name} must {requirement}")
        if not _run_test(name=name):
            print(f"verify FAILED: {name} did not {requirement}", file=sys.stderr)
            return 2
    print("verify: tripwire trio OK")
    return 0


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print result without editing the header")
    parser.add_argument("--start", type=int, default=None, help="starting page count (default: current BIGBSS_PAGES)")
    parser.add_argument("--verify", action="store_true", help="after calibration, run the full tripwire trio")
    arguments = parser.parse_args()

    header_text = HEADER_PATH.read_text()
    match = DEFINE_PATTERN.search(header_text)
    if not match:
        print(f"could not find `#define BIGBSS_PAGES <n>` in {HEADER_PATH}", file=sys.stderr)
        return 1
    current = int(match.group(2))
    start = arguments.start if arguments.start is not None else current
    print(f"current BIGBSS_PAGES = {current}; searching from {start}")

    low, high = _bracket(start=start)
    print(f"bracketed: pass at {low}, fail at {high}")
    ceiling = _bisect(high=high, low=low)
    print(f"page-precise ceiling: BIGBSS_PAGES = {ceiling}")

    if not arguments.dry_run and ceiling != current:
        new_text = DEFINE_PATTERN.sub(rf"\g<1>{ceiling}", header_text)
        HEADER_PATH.write_text(new_text)
        print(f"updated {HEADER_PATH.relative_to(REPO_ROOT)}: {current} -> {ceiling}")

    if arguments.verify:
        return _verify(pages=ceiling)
    return 0


def _probe(*, pages: int) -> bool:
    """Set BIGBSS_PAGES = `pages`, rebuild, run `bigbss`.  True if pass."""
    print(f"  probe pages={pages} ... ", end="", flush=True)
    _set_pages(pages=pages)
    passed = _run_test(name="bigbss")
    print("PASS" if passed else "fail")
    return passed


def _run_test(*, name: str) -> bool:
    """Run a single test via tests/test_programs.py.  True on exit 0."""
    result = subprocess.run(
        ["python3", "tests/test_programs.py", name, "--slow"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        text=True,
    )
    return result.returncode == 0


def _set_pages(*, pages: int) -> None:
    """Rewrite the #define line in bigbss_size.h."""
    header_text = HEADER_PATH.read_text()
    new_text = DEFINE_PATTERN.sub(rf"\g<1>{pages}", header_text)
    HEADER_PATH.write_text(new_text)


if __name__ == "__main__":
    sys.exit(main())
