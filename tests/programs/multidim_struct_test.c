/* Runtime correctness check for cc.py's multidimensional array struct fields.

   ``struct grid { int cells[2][3]; }`` carries a 2-D int field laid out
   row-major within the struct (24 bytes, offsets cells[i][j] at (i*3+j)*4).
   A local ``struct grid g`` is filled with g.cells[i][j] = i*3+j in nested
   ``while``/``i++`` loops, then individual cells are read back via the dot
   form and via a ``struct grid* p = &g`` pointer (arrow form).  The expected
   output is uniquely determined by correct row-major field addressing: any
   column-major or wrong-stride layout would print different numbers.

   cells[2][3] layout (row-major): cells[0][0]=0  cells[0][1]=1  cells[0][2]=2
                                    cells[1][0]=3  cells[1][1]=4  cells[1][2]=5 */

struct grid {
    int cells[2][3];
};

int main() {
    struct grid g;
    struct grid *p;
    int i;
    int j;
    int sum;

    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 3) {
            g.cells[i][j] = i * 3 + j;
            j++;
        }
        i++;
    }

    /* Spot-check via the dot (struct-value) form. */
    printf("dot g[0][0]=%u g[0][2]=%u g[1][0]=%u g[1][2]=%u\n", g.cells[0][0],
           g.cells[0][2], g.cells[1][0], g.cells[1][2]);

    /* Spot-check via the arrow (struct-pointer) form. */
    p = &g;
    printf("arrow p[1][2]=%u p[0][1]=%u\n", p->cells[1][2], p->cells[0][1]);

    /* Sum every element: must be 0+1+2+3+4+5 = 15. */
    sum = 0;
    i = 0;
    while (i < 2) {
        j = 0;
        while (j < 3) {
            sum = sum + g.cells[i][j];
            j++;
        }
        i++;
    }
    printf("sum=%u\n", sum);
    return 0;
}
