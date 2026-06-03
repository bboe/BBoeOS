/* Runtime correctness check for cc.py's multidimensional array INITIALIZERS
   (local + global, with zero-fill of partial initializers).

   A local nested ``int m[2][3] = {{1,2,3},{4,5,6}}`` and a partial
   ``int p[2][2] = {1,2}`` (so p[1][0] and p[1][1] are zero-filled) are read
   back cell-by-cell.  A file-scope ``int g[2][2] = {{10,20},{30,40}}`` proves
   the global init path lays down the same row-major run.  The printed numbers
   are uniquely determined by correct row-major layout AND correct zero-fill:
   any wrong stride, column-major order, or missing zero-fill changes them.

   m[2][3] = {{1,2,3},{4,5,6}}: m[0][0]=1 m[0][2]=3 m[1][0]=4 m[1][2]=6
   p[2][2] = {1,2}:            p[0][0]=1 p[0][1]=2 p[1][0]=0 p[1][1]=0
   g[2][2] = {{10,20},{30,40}}: sum = 10+20+30+40 = 100. */

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to. */
int global_sum();
void run_local();

int g[2][2] = {{10, 20}, {30, 40}};

int main() {
    run_local();
    printf("gsum=%u\n", global_sum());
    return 0;
}

int global_sum() {
    int i;
    int j;
    int sum;

    sum = 0;
    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 2) {
            sum = sum + g[i][j];
            j++;
        }
        i++;
    }
    return sum;
}

void run_local() {
    int m[2][3] = {{1, 2, 3}, {4, 5, 6}};
    int p[2][2] = {1, 2};

    printf("m[0][0]=%u m[0][2]=%u m[1][0]=%u m[1][2]=%u\n", m[0][0], m[0][2],
           m[1][0], m[1][2]);
    printf("p[0][0]=%u p[0][1]=%u p[1][0]=%u p[1][1]=%u\n", p[0][0], p[0][1],
           p[1][0], p[1][1]);
}
