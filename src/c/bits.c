/* Smoke test for cc.py's bitwise operators and their compound-
   assignment forms, plus ``%=`` (the arithmetic compound-assignment
   that exercises the div/remainder path the other compound-assigns
   don't reach) and the memory-direct ALU shapes cc.py's
   ``peephole_memory_arithmetic`` / ``_byte`` emit for a
   globally-stored counter (``add|and|or|sub|xor word [mem], imm``
   for an ``int`` global, ``... byte [mem], imm8`` for a ``uint8_t``
   global).  Every result is printed as unsigned decimal because the
   printf builtin's %x prints only the low byte.  The expected values
   make the verification a simple text match. */

int counter;
uint8_t bcounter;

int main() {
    int a = 61680;  /* 0xF0F0 */
    int b = 4080;   /* 0x0FF0 */
    printf("and  = %u\n", a & b);    /* 0x00F0 =   240 */
    printf("or   = %u\n", a | b);    /* 0xFFF0 = 65520 */
    printf("xor  = %u\n", a ^ b);    /* 0xFF00 = 65280 */
    printf("not  = %u\n", ~a & 65535); /* 0x0F0F =  3855 */
    printf("shl  = %u\n", 1 << 8);   /* 0x0100 =   256 */
    printf("shr  = %u\n", 65280 >> 4); /* 0x0FF0 = 4080 */

    int x = 15;        /* 0x000F */
    x |= 240;          /* |= 0x00F0 → 0x00FF = 255 */
    printf("|=   = %u\n", x);
    x &= 204;          /* &= 0x00CC → 0x00CC = 204 */
    printf("&=   = %u\n", x);
    x ^= 255;          /* ^= 0x00FF → 0x0033 = 51 */
    printf("^=   = %u\n", x);
    x <<= 4;           /* shl 4 → 0x0330 = 816 */
    printf("<<=  = %u\n", x);
    x >>= 2;           /* shr 2 → 0x00CC = 204 */
    printf(">>=  = %u\n", x);

    int y = 50;
    y %= 13;           /* 50 % 13 = 11 */
    printf("%%=   = %u\n", y);
    y -= 5;            /* 11 - 5 = 6 (exercises ``sub word [mem], imm8``) */
    printf("-=   = %u\n", y);

    /* Memory-allocated counter — forces cc.py to emit the
       ``<op> word [_g_counter], imm`` shapes that
       ``peephole_memory_arithmetic`` produces for load/modify/store
       triples on a global.  Each printf between ops clobbers AX so
       the next op must reload from memory. */
    counter = 100;
    printf("g=   = %u\n", counter);
    counter += 5;      /* 105 — exercises ``add word [mem], imm8`` */
    printf("g+=  = %u\n", counter);
    counter |= 16;     /* 105 | 16 = 121 — ``or word [mem], imm8`` */
    printf("g|=  = %u\n", counter);
    counter &= 63;     /* 121 & 63 = 57 — ``and word [mem], imm8`` */
    printf("g&=  = %u\n", counter);
    counter ^= 32;     /* 57 ^ 32 = 25 — ``xor word [mem], imm8`` */
    printf("g^=  = %u\n", counter);

    /* uint8_t global — exercises the byte-width variants via
       ``peephole_memory_arithmetic_byte``.  Use non-1 addends so the
       ``add|sub`` stays as ``add|sub byte [mem], imm`` instead of
       collapsing to ``inc|dec byte [mem]``. */
    bcounter = 100;
    printf("b=   = %u\n", bcounter);
    bcounter += 7;     /* 107 — ``add byte [mem], imm8`` */
    printf("b+=  = %u\n", bcounter);
    bcounter |= 16;    /* 123 — ``or byte [mem], imm8`` */
    printf("b|=  = %u\n", bcounter);
    bcounter &= 63;    /* 59  — ``and byte [mem], imm8`` */
    printf("b&=  = %u\n", bcounter);
    bcounter ^= 8;     /* 51  — ``xor byte [mem], imm8`` */
    printf("b^=  = %u\n", bcounter);
    bcounter -= 5;     /* 46  — ``sub byte [mem], imm8`` */
    printf("b-=  = %u\n", bcounter);
    return 0;
}
