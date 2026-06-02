#!/usr/bin/env python3
"""Byte-exact golden snapshot for Member* / IndexMember* codegen through the Place core.

The original five probes exercise the struct-array shapes: arr[i].field,
arr[i].field[j], arr[i].field = v, and arr[i].field[j] = v as statements,
plus the auxiliary paths these flow through: sizeof(arr[i].field)
(expression-type inference) and (arr[i].field = v) / (arr[i].field[j] = v)
as assignment-as-expression.

The fixture is then extended to capture the CURRENT (legacy) output of the
full Member* family so a later refactor onto the recursive Place abstraction
can prove byte-exactness against this oracle.  The added probes cover: dot
scalar read/store; dot struct-value yield (address-of, &g_outer.in); arrow
scalar read/store; chained o->pin->a read/store; cast-base
((struct flags *)&raw)->hi; inline-array index read/store (variable and
constant subscript); pointer-field index read/store; word (2-byte) inline
index; &member and &member[i]; bitfield read; bitfield general write;
1-bit-literal bitfield write; const-folded bitfield write into a known local
byte; postfix ++ on a member; and prefix -- on a member.

Asserts the cc.py-emitted assembly is identical to a checked-in golden file.
Regenerate the golden deliberately with BBOE_UPDATE_GOLDEN=1 only when output
is intended to change.
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

struct inner { int a; char tag; };
struct outer { struct inner in; struct inner *pin; };
struct flags { int hi; unsigned char a : 1; unsigned char b : 3; unsigned char c : 4; };
struct buf { int n; char data[8]; char *p; unsigned short w[4]; };

struct outer g_outer;
struct flags g_flags;

int probe_dot_read(void) { return g_outer.in.a; }
int probe_dot_store(int v) { g_outer.in.a = v; return g_outer.in.a; }
int probe_arrow_read(struct buf *b) { return b->n; }
int probe_arrow_store(struct buf *b, int v) { b->n = v; return b->n; }
int probe_chain_read(struct outer *o) { return o->pin->a; }
int probe_chain_store(struct outer *o, int v) { o->pin->a = v; return o->pin->a; }
int probe_cast_base(unsigned char raw) { return ((struct flags *)&raw)->hi; }
int probe_inline_index_read(struct buf *b, int i) { return b->data[i]; }
int probe_inline_index_store(struct buf *b, int i, int v) { b->data[i] = v; return 0; }
int probe_inline_index_const(struct buf *b) { return b->data[3]; }
int probe_pointer_index_read(struct buf *b, int i) { return b->p[i]; }
int probe_pointer_index_store(struct buf *b, int i, int v) { b->p[i] = v; return 0; }
int probe_word_inline_index(struct buf *b, int i) { return b->w[i]; }
char *probe_member_addr(struct buf *b) { return &b->n; }
char **probe_member_addr_offset(struct buf *b) { return &b->p; }
char *probe_member_elem_addr(struct buf *b, int i) { return &b->data[i]; }
int probe_bitfield_read(struct flags *f) { return f->b; }
int probe_bitfield_store(struct flags *f, int v) { f->b = v; return 0; }
int probe_bitfield_one_literal(struct flags *f) { f->a = 1; return 0; }
int probe_bitfield_constfold(void) { struct flags local; local.a = 0; local.b = 5; return local.b; }
int probe_member_incdec(struct buf *b) { int pre = b->n++; return pre + b->n; }
int probe_member_predec(struct buf *b) { return --b->n; }
int probe_addr_of_dot(void) { return (int)&g_outer.in; }
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
