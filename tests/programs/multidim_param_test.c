/* Runtime correctness check for cc.py's multidimensional array PARAMETERS and
   pointer-to-array variables.

   A multidim array parameter ``int m[][3]`` decays to a pointer-to-array
   ``int (*m)[3]``: the callee receives the base ADDRESS, and ``m[i][j]`` is
   row-major over the pointee dims (stride_i = 4*3 = 12, stride_j = 4).  The
   same ``sum`` routine is called with a LOCAL ``int m[2][3]`` and a GLOBAL
   ``int g[2][3]``, proving the call-site array→address decay for both storage
   classes.

   An explicit local ``int (*p)[3] = g;`` then reads ``p[1][2]`` back to pin
   the pointer-to-array addressing directly: g[1][2] = 1*3 + 2 = 5, so
   p[1][2] = 5 with offset 12*1 + 4*2 = 20 bytes from the base. */

int g[2][3];

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to. */
int sum(int m[][3], int rows);

int main() {
    int local[2][3];
    int (*p)[3];
    int i;
    int j;

    /* Fill local[i][j] = i*3 + j (values 0..5). */
    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 3) {
            local[i][j] = i * 3 + j;
            j++;
        }
        i++;
    }

    /* Fill the global the same way. */
    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 3) {
            g[i][j] = i * 3 + j;
            j++;
        }
        i++;
    }

    /* sum over 2 rows of 3 = 0+1+2+3+4+5 = 15 for both. */
    printf("local_sum=%u global_sum=%u\n", sum(local, 2), sum(g, 2));

    /* Explicit pointer-to-array decay from the global, then read p[1][2]. */
    p = g;
    printf("p[1][2]=%u\n", p[1][2]);

    return 0;
}

int sum(int m[][3], int rows) {
    int i;
    int j;
    int total;

    total = 0;
    i = 0;
    while (i < rows) {
        j = 0;
        while (j < 3) {
            total = total + m[i][j];
            j++;
        }
        i++;
    }
    return total;
}
