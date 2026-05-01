/* Stress test for the lifted direct-map ceiling.  Allocates an 800 MB
   BSS array and writes/verifies one int per 4 KB page (~204,800
   iterations per pass), exercising every frame the program's user PD
   maps.  Pairs with the `bigbss800` entry in tests/test_programs.py,
   which boots with -m 1024 to give the user pool enough room.

   800 MB sits below both ceilings:
     * user-virt: PROGRAM_BASE (0x08048000) + 800 MB = 0x3A048000,
       under the stack guard at 0x3FFE0000.
     * direct map: 800 MB < 1 GB cap (LAST_KERNEL_PDE = 1024). */

#define PAGE_BYTES 4096
#define PAGE_COUNT (800 * 1024 * 1024 / PAGE_BYTES)
#define INTS_PER_PAGE (PAGE_BYTES / 4)
#define BIG_SIZE_INTS (PAGE_COUNT * INTS_PER_PAGE)

int big_array[BIG_SIZE_INTS];

int main() {
    printf("bigbss800: writing pattern\n");
    int i = 0;
    int idx = 0;
    while (i < PAGE_COUNT) {
        big_array[idx] = i;
        idx = idx + INTS_PER_PAGE;
        i = i + 1;
    }
    printf("bigbss800: verifying pattern\n");
    i = 0;
    idx = 0;
    while (i < PAGE_COUNT) {
        if (big_array[idx] != i) {
            printf("bigbss800: FAIL at page %d\n", i);
            return 1;
        }
        idx = idx + INTS_PER_PAGE;
        i = i + 1;
    }
    printf("bigbss800: OK\n");
    return 0;
}
