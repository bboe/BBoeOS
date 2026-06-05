#!/usr/bin/env python3
"""Per-function register-home parity gate.

Compiles every userland C source and compares each function's {var: register}
home map against tests/golden/cc_register_homes_baseline.json.  Refresh the
golden deliberately with BBOE_UPDATE_HOMES=1 only when an assignment change is
intended and byte-verified (tests/test_cc_function_sizes.py is the hard gate).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cc.cli import compile_source_homes  # noqa: E402 — needs ROOT on sys.path first

GOLDEN = ROOT / "tests" / "golden" / "cc_register_homes_baseline.json"
SOURCES = sorted({*ROOT.glob("user/libbboeos/*.c"), *ROOT.glob("user/programs/*.c")})

# Keys are repo-relative POSIX paths matching the baseline JSON keys
# (e.g. "user/libbboeos/stdio.c").  Inner dict maps function name -> reason string.
# Documented byte-neutral register-identity exceptions: {source: {function: reason}}.
IDENTITY_EXCEPTIONS: dict[str, dict[str, str]] = {}


def _check_baseline(
    baseline: dict[str, dict[str, dict[str, str]]],
    current: dict[str, dict[str, dict[str, str]]],
) -> list[str]:
    """Return failures for functions present in baseline but wrong or missing in current."""
    failures: list[str] = []
    for source, functions in baseline.items():
        for name, homes in functions.items():
            got = current.get(source, {}).get(name)
            if got is None:
                failures.append(f"{source}:{name}: missing from current build")
            elif got != homes and name not in IDENTITY_EXCEPTIONS.get(source, {}):
                failures.append(f"{source}:{name}: homes {homes} -> {got}")
    return failures


def _check_forward(
    baseline: dict[str, dict[str, dict[str, str]]],
    current: dict[str, dict[str, dict[str, str]]],
) -> list[str]:
    """Return failures for functions present in current but absent from baseline."""
    return [
        f"{source}:{name}: new function not in baseline (run BBOE_UPDATE_HOMES=1)"
        for source, functions in current.items()
        for name in functions
        if name not in baseline.get(source, {})
    ]


def current_homes() -> dict[str, dict[str, dict[str, str]]]:
    """Return ``{repo_relative_source: {function: {var: register}}}`` for the corpus."""
    result: dict[str, dict[str, dict[str, str]]] = {}
    for source in SOURCES:
        rel = str(source.relative_to(ROOT))
        result[rel] = compile_source_homes(source=source)
    return result


def main() -> int:
    """Run the parity gate, or refresh the golden when BBOE_UPDATE_HOMES=1."""
    current = current_homes()
    if os.environ.get("BBOE_UPDATE_HOMES") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"wrote {GOLDEN}")
        return 0
    baseline = json.loads(GOLDEN.read_text())
    failures = _check_baseline(baseline, current)
    failures.extend(_check_forward(baseline, current))
    if failures:
        print("REGISTER-HOME PARITY FAILURES:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"register-home parity OK ({len(SOURCES)} sources)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
