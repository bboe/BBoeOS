/* user/libbboeos/test_program/hello.c — on-OS smoke test for libbboeos.a.
 *
 * Exercises printf (with several format specifiers), malloc/free,
 * setjmp/longjmp, and program exit through _start's call-main-then-exit
 * path.  Built by user/libbboeos/Makefile against user/libbboeos/program.ld and
 * dropped on the disk image as bin/hello; verified by
 * tests/test_libbboeos_qemu.py. */

#include <dirent.h>
#include <errno.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static jmp_buf env;

static void crash(void) {
    longjmp(env, 42);
}

static void exercise_dirent(void) {
    DIR *directory = opendir(".");
    if (directory == NULL) {
        printf("[libbboeos] FAIL: opendir . returned NULL\n");
        _exit(1);
    }
    int count = 0;
    int saw_bin = 0;
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (strcmp(entry->d_name, "bin") == 0) {
            saw_bin = 1;
        }
        count = count + 1;
    }
    if (!saw_bin) {
        printf("[libbboeos] FAIL: did not see 'bin' subdirectory\n");
        _exit(1);
    }
    rewinddir(directory);
    int recount = 0;
    while ((entry = readdir(directory)) != NULL) {
        recount = recount + 1;
    }
    if (count != recount) {
        printf("[libbboeos] FAIL: rewinddir count mismatch %d vs %d\n", count,
               recount);
        _exit(1);
    }
    if (closedir(directory) != 0) {
        printf("[libbboeos] FAIL: closedir returned nonzero\n");
        _exit(1);
    }
    if (opendir("nonexistent_path_xyz") != NULL) {
        printf("[libbboeos] FAIL: opendir on missing path returned non-NULL\n");
        _exit(1);
    }
    printf("[libbboeos] dirent: %d entries, rewind ok\n", count);
}

int main(void) {
    printf("[libbboeos] hello\n");

    char *p = malloc(64);
    strcpy(p, "malloc-works");
    printf("[libbboeos] %s\n", p);
    free(p);

    printf("[libbboeos] %d %u %x %s\n", -1, 4000000000U, 0xCAFE, "ok");

    int v = setjmp(env);
    if (v == 0) {
        crash();
    } else {
        printf("[libbboeos] longjmp returned %d\n", v);
    }

    exercise_dirent();

    printf("[libbboeos] done\n");
    return 0;
}
