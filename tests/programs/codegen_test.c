/* codegen_test — consolidated cc.py codegen smoke tests.
 *
 * Two modes, each previously its own program:
 *
 *   bits  — bitwise operators, compound-assigns, ``%=``, and the
 *           memory-direct ALU shapes peephole_memory_arithmetic /
 *           peephole_memory_arithmetic_byte emit for word / byte
 *           globals.  Every result printed as unsigned decimal so a
 *           text-diff match catches regressions.  Previously bits.c.
 *
 *   bool  — booleanization of comparison BinOps used as expression
 *           values (``int x = (expr <op> val);`` / call args / sub-
 *           expressions).  Prior to the fix the codegen dropped the
 *           compare result; every booleanization landed 0.  Each line
 *           prints observed + expected so regressions are obvious.
 *           Previously booltest.c. */

uint8_t bcounter;
int counter;

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to.
   cc.py's whole-file pre-pass resolves these without prototypes. */
void mode_bits();
void mode_bool();
int string_equal(char *left, char *right);

int main(int argc, char *argv[]) {
    if (argc < 2) {
        die("codegen_test: pass a mode\n");
    }
    char *mode = argv[1];
    if (string_equal(mode, "bits")) {
        mode_bits();
    } else if (string_equal(mode, "bool")) {
        mode_bool();
    } else {
        die("codegen_test: unknown mode\n");
    }
    return 0;
}

void mode_bits() {
    int a = 61680;                     /* 0xF0F0 */
    int b = 4080;                      /* 0x0FF0 */
    printf("and  = %u\n", a & b);      /* 0x00F0 =   240 */
    printf("or   = %u\n", a | b);      /* 0xFFF0 = 65520 */
    printf("xor  = %u\n", a ^ b);      /* 0xFF00 = 65280 */
    printf("not  = %u\n", ~a & 65535); /* 0x0F0F =  3855 */
    printf("shl  = %u\n", 1 << 8);     /* 0x0100 =   256 */
    printf("shr  = %u\n", 65280 >> 4); /* 0x0FF0 = 4080 */

    int x = 15; /* 0x000F */
    x |= 240;   /* |= 0x00F0 → 0x00FF = 255 */
    printf("|=   = %u\n", x);
    x &= 204; /* &= 0x00CC → 0x00CC = 204 */
    printf("&=   = %u\n", x);
    x ^= 255; /* ^= 0x00FF → 0x0033 = 51 */
    printf("^=   = %u\n", x);
    x <<= 4; /* shl 4 → 0x0330 = 816 */
    printf("<<=  = %u\n", x);
    x >>= 2; /* shr 2 → 0x00CC = 204 */
    printf(">>=  = %u\n", x);

    int y = 50;
    y %= 13; /* 50 % 13 = 11 */
    printf("%%=   = %u\n", y);
    y -= 5; /* 11 - 5 = 6 (exercises ``sub word [mem], imm8``) */
    printf("-=   = %u\n", y);

    /* Memory-allocated counter — forces cc.py to emit the
       ``<op> word [_g_counter], imm`` shapes that
       peephole_memory_arithmetic produces for load/modify/store
       triples on a global.  Each printf between ops clobbers AX so
       the next op must reload from memory. */
    counter = 100;
    printf("g=   = %u\n", counter);
    counter += 5; /* 105 — ``add word [mem], imm8`` */
    printf("g+=  = %u\n", counter);
    counter |= 16; /* 105 | 16 = 121 — ``or word [mem], imm8`` */
    printf("g|=  = %u\n", counter);
    counter &= 63; /* 121 & 63 = 57 — ``and word [mem], imm8`` */
    printf("g&=  = %u\n", counter);
    counter ^= 32; /* 57 ^ 32 = 25 — ``xor word [mem], imm8`` */
    printf("g^=  = %u\n", counter);

    /* uint8_t global — exercises the byte-width variants via
       peephole_memory_arithmetic_byte.  Use non-1 addends so the
       ``add|sub`` stays as ``add|sub byte [mem], imm`` instead of
       collapsing to ``inc|dec byte [mem]``. */
    bcounter = 100;
    printf("b=   = %u\n", bcounter);
    bcounter += 7; /* 107 — ``add byte [mem], imm8`` */
    printf("b+=  = %u\n", bcounter);
    bcounter |= 16; /* 123 — ``or byte [mem], imm8`` */
    printf("b|=  = %u\n", bcounter);
    bcounter &= 63; /* 59  — ``and byte [mem], imm8`` */
    printf("b&=  = %u\n", bcounter);
    bcounter ^= 8; /* 51  — ``xor byte [mem], imm8`` */
    printf("b^=  = %u\n", bcounter);
    bcounter -= 5; /* 46  — ``sub byte [mem], imm8`` */
    printf("b-=  = %u\n", bcounter);
}

void mode_bool() {
    int a = 7;
    int b = 7;
    int c = 12;

    int eq_true = (a == b);  /* 1 */
    int eq_false = (a == c); /* 0 */
    int ne_true = (a != c);  /* 1 */
    int ne_false = (a != b); /* 0 */
    int lt_true = (a < c);   /* 1 */
    int lt_false = (c < a);  /* 0 */
    int le_true = (a <= b);  /* 1 */
    int le_false = (c <= a); /* 0 */
    int gt_true = (c > a);   /* 1 */
    int gt_false = (a > c);  /* 0 */
    int ge_true = (a >= b);  /* 1 */
    int ge_false = (a >= c); /* 0 */

    printf("eq_true  = %u\n", eq_true);
    printf("eq_false = %u\n", eq_false);
    printf("ne_true  = %u\n", ne_true);
    printf("ne_false = %u\n", ne_false);
    printf("lt_true  = %u\n", lt_true);
    printf("lt_false = %u\n", lt_false);
    printf("le_true  = %u\n", le_true);
    printf("le_false = %u\n", le_false);
    printf("gt_true  = %u\n", gt_true);
    printf("gt_false = %u\n", gt_false);
    printf("ge_true  = %u\n", ge_true);
    printf("ge_false = %u\n", ge_false);

    /* Arithmetic on the boolean result: ``a == b`` must actually be
       ``1`` (not 0) for the sum to come out right. */
    int sum = (a == b) + (c > a) + (a != c); /* 3 */
    printf("sum      = %u\n", sum);
}

int string_equal(char *left, char *right) {
    int index = 0;
    while (left[index] != '\0' && right[index] != '\0') {
        if (left[index] != right[index]) {
            return 0;
        }
        index = index + 1;
    }
    return left[index] == right[index];
}
