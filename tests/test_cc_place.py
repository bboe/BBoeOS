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

The fixture is further extended to capture the legacy output of the full
dereference family ahead of its Plan 3 conversion onto the recursive Place
abstraction.  These deref probes cover: *p read/store for char (byte), int
(full) and unsigned short (word) pointees; the *(T *)&local AddressOf-of-local
fast path for byte and word widths (read and write); the general *(T *)e
read/store path for byte and word widths; a[i][j] double indexing for byte
(char *[]), full (int *[]) and word (unsigned short *[]) pointees with constant,
variable and general inner indices; postfix/prefix ++/-- deref reads and stores;
the purity traps of a deref read and a double-index read used in an if
condition; (*p = v), (*p++ = v) and (*(T *)e = v) assignment-as-expression
values; and sizeof(*p) / sizeof(*(unsigned short *)e).

The fixture is further extended to capture the legacy output of the three
operation nodes Plan 4 folds onto the recursive Place abstraction: AddressOf
(&x), IncrementDecrement (x++/++x/x--/--x) and IndexedCall (arr[i](args)).
These Plan 4 probes cover: &x address-of a global scalar and a local scalar,
plus &x inside sizeof; ++/-- in all four positions (postfix/prefix x++/--x)
used as expressions whose value is consumed for both a local and a global int,
and as value-discarding statements for a local and a global int; and indexed
function-pointer calls through a global array and a local array, with both a
constant and a variable index, exercised as a statement and as an expression
whose result is used.  The legacy parser rejects a function-pointer-array
*parameter* (int (*t[4])(int) as a formal parameter raises "expected RPAREN,
got LBRACKET"), so the local-array probes declare a local int (*t[4])(int) and
assign its elements from a file-scope source array before calling through it.

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
int probe_member_increment_decrement(struct buf *b) { int pre = b->n++; return pre + b->n; }
int probe_member_predec(struct buf *b) { return --b->n; }
int probe_addr_of_dot(void) { return (int)&g_outer.in; }

/* --- Plan 3 deref-family probes (captured from the legacy compiler) --- */
struct flags2 { unsigned char hi; unsigned char lo; };

int probe_deref_read_char(char *p) { return *p; }
int probe_deref_read_int(int *p) { return *p; }
int probe_deref_read_ushort(unsigned short *p) { return *p; }
int probe_deref_store_char(char *p, int v) { *p = v; return *p; }
int probe_deref_store_int(int *p, int v) { *p = v; return *p; }
int probe_deref_store_ushort(unsigned short *p, int v) { *p = v; return *p; }

int probe_cast_deref_uchar_local(void) { struct flags2 s; s.hi = 7; return *(unsigned char *)&s; }
int probe_cast_deref_ushort_local(void) { int box; box = 0; return *(unsigned short *)&box; }
int probe_cast_deref_uchar_expr(char *base, int off) { return *(unsigned char *)(base + off); }
int probe_cast_deref_ushort_expr(char *base, int off) { return *(unsigned short *)(base + off); }
void probe_cast_deref_store_uchar_local(int v) { struct flags2 s; *(unsigned char *)&s = v; }
void probe_cast_deref_store_uchar_expr(char *base, int off, int v) { *(unsigned char *)(base + off) = v; }
void probe_cast_deref_store_ushort_expr(char *base, int off, int v) { *(unsigned short *)(base + off) = v; }

char *names[4];
int *ints[4];
unsigned short *words[4];
int probe_double_index_byte_const(void) { return names[1][2]; }
int probe_double_index_byte_var(int i, int j) { return names[i][j]; }
int probe_double_index_byte_expr(int i, int j) { return names[i][j + 1]; }
int probe_double_index_int_const(void) { return ints[1][2]; }
void probe_double_index_int_store_const(int i, int v) { ints[i][0] = v; }
void probe_double_index_int_store_var(int i, int j, int v) { ints[i][j] = v; }
int probe_double_index_int_var(int i, int j) { return ints[i][j]; }
int probe_double_index_word_var(int i, int j) { return words[i][j]; }

int probe_deref_postinc_read(int *p) { int a = *p++; return a; }
int probe_deref_preinc_read(int *p) { int a = *++p; return a; }
int probe_deref_postdec_read(char *p) { int a = *p--; return a; }
int probe_deref_predec_read(int *p) { int a = *--p; return a; }
void probe_deref_postinc_store(char *out, int v) { *out++ = v; }
void probe_deref_preinc_store(int *out, int v) { *++out = v; }

int probe_deref_in_if(int *p) { if (*p) { return 1; } return 0; }
int probe_double_index_in_if(int i, int j) { if (ints[i][j]) { return 1; } return 0; }
int probe_deref_assign_expr(int *p, int v) { int y = (*p = v); return y; }
int probe_deref_incassign_expr(char *out, int v) { int y = (*out++ = v); return y; }
int probe_cast_deref_assign_expr(char *base, int off, int v) { int y = (*(unsigned char *)(base + off) = v); return y; }
int probe_sizeof_deref(int *p) { return sizeof(*p); }
int probe_sizeof_cast_deref_expr(char *base, int off) { return sizeof(*(unsigned short *)(base + off)); }

/* --- Plan 4 fold probes (captured from the legacy compiler) --- */
int g_counter;
int (*g_fptable[4])(int);
int (*g_fptable_src[4])(int);

int probe_addr_of_global(void) { return (int)&g_counter; }
int probe_addr_of_local(void) { int local; local = 0; return (int)&local; }
int probe_sizeof_addr(int n) { return sizeof(&n); }

int probe_postinc_expr(int n) { int a = n++; return a + n; }
int probe_preinc_expr(int n) { int a = ++n; return a + n; }
int probe_postdec_expr(int n) { int a = n--; return a + n; }
int probe_predec_expr(int n) { int a = --n; return a + n; }
int probe_postinc_expr_global(void) { int a = g_counter++; return a + g_counter; }
int probe_preinc_expr_global(void) { int a = ++g_counter; return a + g_counter; }
void probe_postinc_stmt(void) { g_counter++; }
void probe_predec_stmt(void) { --g_counter; }
void probe_postinc_stmt_local(int n) { n++; }
void probe_predec_stmt_local(int n) { --n; }

int probe_indexed_call_global_const(int x) { return g_fptable[1](x); }
int probe_indexed_call_global_var(int i, int x) { return g_fptable[i](x); }
int probe_indexed_call_global_const_exprval(int x) { int r = g_fptable[1](x); return r; }
int probe_indexed_call_global_var_exprval(int i, int x) { int r = g_fptable[i](x); return r; }
void probe_indexed_call_global_stmt(int x) { g_fptable[0](x); }
int probe_indexed_call_local_const(int x) { int (*t[4])(int); t[2] = g_fptable_src[2]; return t[2](x); }
int probe_indexed_call_local_var(int i, int x) { int (*t[4])(int); t[i] = g_fptable_src[i]; return t[i](x); }
void probe_indexed_call_local_stmt(int x) { int (*t[4])(int); t[0] = g_fptable_src[0]; t[0](x); }

/* --- Plan 4 new-shape probes (no legacy oracle; eyeballed + runtime-verified) --- */
int g_arr[8];
int *g_rows[4];

int probe_addr_deref(int *p) { return (int)&*p; }
int probe_named_array_postinc(int i) { g_arr[i] = 5; int pre = g_arr[i]++; return pre + g_arr[i]; }
int probe_named_array_predec(int i) { g_arr[i] = 5; return --g_arr[i]; }
void probe_named_array_postinc_stmt(int i) { g_arr[i]++; }
int probe_double_index_postinc(int i, int j) { return g_rows[i][j]++; }
void probe_double_index_postinc_stmt(int i, int j) { g_rows[i][j]++; }
int probe_double_index_preinc(int i, int j) { return ++g_rows[i][j]; }
void probe_named_array_predec_stmt(int i) { --g_arr[i]; }
void probe_double_index_preinc_stmt(int i, int j) { ++g_rows[i][j]; }
int probe_call_through_ptr(int (*fp)(int), int x) { return (*fp)(x); }

/* --- Plan 6 resolver-store probes (no legacy oracle; eyeballed + runtime-verified) --- */
/* probe_chained_bitfield_store: outer.inner_flags.b = v via the
   _emit_member_scalar_resolved_store accumulator-clobbering path
   (MemberPlace base → PlaceLoad of inner struct address into BX) with a
   bitfield terminal (_emit_resolved_field_store → _emit_bitfield_write).
   The legacy _emit_member_chained_store had no bitfield branch; this path
   is strictly more correct and was previously untested. */
struct flags_wrapper { struct flags inner_flags; };
int probe_chained_bitfield_store(int v) { struct flags_wrapper w; w.inner_flags.b = v; return w.inner_flags.b; }
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
