/* Demonstrates a file-scope int array with a brace initializer.  The
   elements are emitted as ``_g_fib: dw 1, 1, 2, ...`` at NASM assemble
   time, so sizeof(fib) folds to the constant ``10*2`` in the loop
   bound. */

int fib[] = {1, 1, 2, 3, 5, 8, 13, 21, 34, 55};

int main() {
    int i = 0;
    while (i < sizeof(fib) / sizeof(int)) {
        printf("fib[%d] = %d\n", i, fib[i]);
        i += 1;
    }
}
