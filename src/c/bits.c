/* Smoke test for cc.py's bitwise operators and their compound-
   assignment forms, plus ``%=`` (the arithmetic compound-assignment
   that exercises the div/remainder path the other compound-assigns
   don't reach).  Every result is printed as unsigned decimal because
   the printf builtin's %x prints only the low byte.  The expected
   values make the verification a simple text match. */

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
    return 0;
}
