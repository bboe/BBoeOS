#!/usr/bin/env python3
"""Byte-exact golden snapshot for IndexMember* codegen through the Place core.

Compiles a fixture exercising arr[i].field, arr[i].field[j],
arr[i].field = v, and arr[i].field[j] = v as statements, plus the
auxiliary paths these shapes flow through: sizeof(arr[i].field)
(expression-type inference) and (arr[i].field = v) / (arr[i].field[j] = v)
as assignment-as-expression.  Asserts the cc.py-emitted assembly is
identical to a checked-in golden file.  Regenerate the golden deliberately
with BBOE_UPDATE_GOLDEN=1 only when output is intended to change.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CC = REPO_ROOT / "cc.py"
LIBBBOEOS_INCLUDE = REPO_ROOT / "user" / "libbboeos" / "include"
GOLDEN = REPO_ROOT / "tests" / "golden" / "cc_place_index_member.asm"

FIXTURE = """\
struct point { int x; int y; char tag; char path[4]; };
struct point points[8];

struct wrec { int x; unsigned short w[4]; };
struct wrec wrecs[8];

int probe(int i, int j, int v) {
    points[i].x = v;
    points[i].path[j] = v;
    int a = points[i].y;
    int b = points[i].path[j];
    return a + b;
}

int probe_word_member(int i, int j, int v) {
    wrecs[i].w[j] = v;
    return wrecs[i].w[j];
}

int probe_sizeof(int i) {
    return sizeof(points[i].x);
}

int probe_assign_expr(int i, int v) {
    int y = (points[i].x = v);
    return y;
}

int probe_assign_elem_expr(int i, int j, int v) {
    int y = (points[i].path[j] = v);
    return y;
}
"""


def emit_asm(*, work: Path) -> str:
    """Compile the fixture with cc.py and return the emitted assembly text."""
    source_path = work / "index_member.c"
    asm_path = work / "index_member.asm"
    source_path.write_text(FIXTURE)
    subprocess.run(
        ["python3", str(CC), "--bits", "32", "-I", str(LIBBBOEOS_INCLUDE), str(source_path), str(asm_path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return asm_path.read_text()


def main() -> int:
    """Run the golden snapshot test, or regenerate the golden when BBOE_UPDATE_GOLDEN=1."""
    with tempfile.TemporaryDirectory(prefix="test_cc_place_") as temporary_directory:
        asm = emit_asm(work=Path(temporary_directory))
    if os.environ.get("BBOE_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(asm)
        print(f"WROTE golden {GOLDEN}")
        return 0
    expected = GOLDEN.read_text()
    if asm == expected:
        print("PASS  index_member golden byte-identical")
        return 0
    print("FAIL  index_member golden differs")
    paired = itertools.zip_longest(asm.splitlines(), expected.splitlines(), fillvalue="<missing>")
    for line_number, (got, want) in enumerate(paired, 1):
        if got != want:
            print(f"  line {line_number}: got {got!r} want {want!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
