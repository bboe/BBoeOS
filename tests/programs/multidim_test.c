/* Runtime correctness check for cc.py's contiguous multidimensional array
   row-major addressing.  A 2-D ``int m[2][3]`` and a 3-D ``int c[2][2][2]``
   are filled with index-derived values in nested loops, then individual cells
   are read back and summed.  The expected output is uniquely determined by
   correct row-major layout: any column-major or stride-incorrect addressing
   would write and read different values and produce different printed numbers.

   m[2][3] layout (row-major): m[0][0]=0  m[0][1]=1  m[0][2]=2
                                m[1][0]=3  m[1][1]=4  m[1][2]=5
   Spot checks: m[0][0]=0, m[0][2]=2, m[1][0]=3, m[1][2]=5, sum=15.

   c[2][2][2] layout (row-major): c[i][j][k] = i*4 + j*2 + k  (0..7)
   Sum = 0+1+2+3+4+5+6+7 = 28. */

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to.
   cc.py's whole-file pre-pass resolves these without prototypes. */
void run_2d();
void run_3d();

int main() {
    run_2d();
    run_3d();
    return 0;
}

void run_2d() {
    int m[2][3];
    int i;
    int j;
    int sum;

    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 3) {
            m[i][j] = i * 3 + j;
            j++;
        }
        i++;
    }

    /* Spot-check individual cells to confirm row-major addresses. */
    printf("m[0][0]=%u m[0][2]=%u m[1][0]=%u m[1][2]=%u\n", m[0][0], m[0][2],
           m[1][0], m[1][2]);

    /* Sum every element: must be 0+1+2+3+4+5 = 15. */
    sum = 0;
    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 3) {
            sum = sum + m[i][j];
            j++;
        }
        i++;
    }
    printf("sum2d=%u\n", sum);
}

void run_3d() {
    int c[2][2][2];
    int i;
    int j;
    int k;
    int sum;

    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 2) {
            k = 0;
            while (k < 2) {
                c[i][j][k] = i * 4 + j * 2 + k;
                k++;
            }
            j++;
        }
        i++;
    }

    /* Sum every element: must be 0+1+2+3+4+5+6+7 = 28. */
    sum = 0;
    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 2) {
            k = 0;
            while (k < 2) {
                sum = sum + c[i][j][k];
                k++;
            }
            j++;
        }
        i++;
    }
    printf("sum3d=%u\n", sum);
}
