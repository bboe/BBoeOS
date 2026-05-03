/* tools/libc/test_program/hello.c — on-OS smoke test for libbboeos.a.
 *
 * Exercises printf (with several format specifiers), malloc/free,
 * setjmp/longjmp, and program exit through _start's call-main-then-exit
 * path.  Built by tools/libc/Makefile against tools/libc/program.ld and
 * dropped on the disk image as bin/hello; verified by
 * tests/test_libbboeos_qemu.py. */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>

static jmp_buf env;

static void crash(void) {
    longjmp(env, 42);
}

int main(void) {
    printf("[bboeos libc] hello\n");

    char *p = malloc(64);
    strcpy(p, "malloc-works");
    printf("[bboeos libc] %s\n", p);
    free(p);

    printf("[bboeos libc] %d %u %x %s\n", -1, 4000000000U, 0xCAFE, "ok");

    int v = setjmp(env);
    if (v == 0) {
        crash();
    } else {
        printf("[bboeos libc] longjmp returned %d\n", v);
    }

    printf("[bboeos libc] done\n");
    return 0;
}
