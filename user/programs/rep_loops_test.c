/* Runtime correctness check for cc.py's rep-string loop rewrite: each
   unit-stride fill / copy in run_fills() is recognized and lowered to a
   ``rep stos`` / ``rep movs`` (widths 1 / 2 / 4).  The final loop has a
   signed counter and a negative bound, exercising the ``n <= 0`` guard
   that must skip the rep entirely (zero iterations, ``b`` unchanged).

   The work lives in a helper, not main(): codegen routes main() through
   the AST path, but every other function through the IR path where the
   rep-string optimizer runs — so the rewrite only fires off main().  Each
   loop also uses its own counter so the induction variable is dead after
   the loop (a shared, re-initialized counter reads as live in the next
   loop's header and the matcher conservatively declines to rewrite). */

/* Forward declaration — clang requires it since main() is sorted
   alphabetically and lands ahead of run_fills(). */
void run_fills();

int main() {
    run_fills();
    return 0;
}

void run_fills() {
    unsigned char b[8];
    unsigned short h[8];
    unsigned int w[8];
    unsigned char cb[8];
    int i;
    int j;
    int k;
    int m;
    int n;

    for (i = 0; i < 8; i++)
        b[i] = 0x41; /* rep stosb */
    for (j = 0; j < 8; j++)
        h[j] = 0x1234; /* rep stosw */
    for (k = 0; k < 8; k++)
        w[k] = 0xdeadbeef; /* rep stosd */
    for (m = 0; m < 8; m++)
        cb[m] = b[m]; /* rep movsb */
    for (n = 0; n < -3; n++)
        b[n] = 0; /* signed guard: zero iterations, b unchanged */

    /* Print full-width decimal: the shared (FUNCTION_PRINTF) %x renders
       only the low byte in uppercase, which would mask the high bytes of
       the stosw / stosd fills.  %u walks the full 32-bit value, so a
       truncated or mis-widened rep store would change the printed number.
       Expected: 65 (0x41) 4660 (0x1234) 3735928559 (0xdeadbeef) 65 65. */
    printf("%u %u %u %u %u\n", b[7], h[7], w[7], cb[0], b[0]);
}
