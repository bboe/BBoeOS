"""Pytest unit tests for the libbboeos.a libc shim's pure functions.

Compiles tiny C programs that include both the system header and our
implementation (under bboeos_*-renamed names), and asserts they agree
across the input range.  Runs in <1 s; fast iteration.

Builds with the system cc; libbboeos.a is *not* used here — we feed
the .c sources to system cc with our header path shadowing the system
one for the function-name conflict resolution.

Run with: ``pytest tests/unit/test_libbboeos.py``
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIBC = REPO_ROOT / "tools" / "libc"


def _compile_and_run(*, c_source: str, extra_sources: list[Path]) -> str:
    """Compile c_source against extra_sources via host clang, run, return stdout."""
    # Resolve the placeholder ../tools/libc/... include path to an absolute
    # path so the temp file's directory location does not matter.
    c_source = c_source.replace("../tools/libc/", str(LIBC) + "/")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "t.c"
        out = Path(td) / "t"
        src.write_text(c_source)
        subprocess.check_call([
            "clang",
            "-O0",
            "-Wall",
            "-Werror",
            "-I",
            str(LIBC / "include"),
            str(src),
            *[str(p) for p in extra_sources],
            "-o",
            str(out),
            "-lm",
        ])
        return subprocess.check_output([str(out)], text=True)


def test_ctype_matches_system() -> None:
    """Verify our ctype.c agrees with the host libc on every byte 0..255."""
    src = r"""
#include <ctype.h>      /* SYSTEM */
#include <stdio.h>
/* Pull our impls under different names so they don't collide. */
#define isalnum  bboeos_isalnum
#define isalpha  bboeos_isalpha
#define iscntrl  bboeos_iscntrl
#define isdigit  bboeos_isdigit
#define islower  bboeos_islower
#define isprint  bboeos_isprint
#define ispunct  bboeos_ispunct
#define isspace  bboeos_isspace
#define isupper  bboeos_isupper
#define isxdigit bboeos_isxdigit
#define tolower  bboeos_tolower
#define toupper  bboeos_toupper
#include "../tools/libc/ctype.c"
#undef isalnum
#undef isalpha
#undef iscntrl
#undef isdigit
#undef islower
#undef isprint
#undef ispunct
#undef isspace
#undef isupper
#undef isxdigit
#undef tolower
#undef toupper
#include <ctype.h>
int main(void) {
    int fail = 0;
    for (int c = 0; c < 256; c++) {
        if (!!isalnum(c)  != !!bboeos_isalnum(c))  { printf("isalnum %d\n",  c); fail++; }
        if (!!isalpha(c)  != !!bboeos_isalpha(c))  { printf("isalpha %d\n",  c); fail++; }
        if (!!iscntrl(c)  != !!bboeos_iscntrl(c))  { printf("iscntrl %d\n",  c); fail++; }
        if (!!isdigit(c)  != !!bboeos_isdigit(c))  { printf("isdigit %d\n",  c); fail++; }
        if (!!islower(c)  != !!bboeos_islower(c))  { printf("islower %d\n",  c); fail++; }
        if (!!isprint(c)  != !!bboeos_isprint(c))  { printf("isprint %d\n",  c); fail++; }
        if (!!ispunct(c)  != !!bboeos_ispunct(c))  { printf("ispunct %d\n",  c); fail++; }
        if (!!isspace(c)  != !!bboeos_isspace(c))  { printf("isspace %d\n",  c); fail++; }
        if (!!isupper(c)  != !!bboeos_isupper(c))  { printf("isupper %d\n",  c); fail++; }
        if (!!isxdigit(c) != !!bboeos_isxdigit(c)) { printf("isxdigit %d\n", c); fail++; }
        if (tolower(c)    != bboeos_tolower(c))    { printf("tolower %d\n",  c); fail++; }
        if (toupper(c)    != bboeos_toupper(c))    { printf("toupper %d\n",  c); fail++; }
    }
    printf("fail=%d\n", fail);
    return fail != 0;
}
"""
    out = _compile_and_run(c_source=src, extra_sources=[LIBC / "ctype.c"])
    assert out.strip().endswith("fail=0"), out


def test_math_matches_system() -> None:
    """Verify our math.c agrees with the host libm at representative inputs."""
    src = r"""
#include <math.h>
#include <stdio.h>
#define atan2 bboeos_atan2
#define cos   bboeos_cos
#define fabs  bboeos_fabs
#define floor bboeos_floor
#define pow   bboeos_pow
#define sin   bboeos_sin
#define sqrt  bboeos_sqrt
#include "../tools/libc/math.c"
#undef atan2
#undef cos
#undef fabs
#undef floor
#undef pow
#undef sin
#undef sqrt
#include <math.h>

static int close_enough(double a, double b) {
    double d = a - b;
    if (d < 0) d = -d;
    return d < 1e-9 || (a != 0 && d / fabs(a) < 1e-9);
}

int main(void) {
    int fail = 0;
    double xs[] = {0.0, 0.5, 1.0, -1.0, 3.14159265, -3.14159265, 100.0};
    for (size_t i = 0; i < sizeof(xs)/sizeof(*xs); i++) {
        double x = xs[i];
        if (!close_enough(bboeos_cos(x),   cos(x)))           { printf("cos %f\n",   x); fail++; }
        if (!close_enough(bboeos_fabs(x),  fabs(x)))          { printf("fabs %f\n",  x); fail++; }
        if (!close_enough(bboeos_floor(x), floor(x)))         { printf("floor %f\n", x); fail++; }
        if (!close_enough(bboeos_sin(x),   sin(x)))           { printf("sin %f\n",   x); fail++; }
        if (x >= 0 && !close_enough(bboeos_sqrt(x), sqrt(x))) { printf("sqrt %f\n",  x); fail++; }
    }
    if (!close_enough(bboeos_atan2(-1,-1), atan2(-1,-1))) { printf("atan2 q3\n"); fail++; }
    if (!close_enough(bboeos_atan2(-1, 1), atan2(-1, 1))) { printf("atan2 q4\n"); fail++; }
    if (!close_enough(bboeos_atan2( 1,-1), atan2( 1,-1))) { printf("atan2 q2\n"); fail++; }
    if (!close_enough(bboeos_atan2( 1, 1), atan2( 1, 1))) { printf("atan2 q1\n"); fail++; }
    if (!close_enough(bboeos_pow(1.5, 3),  pow(1.5, 3)))  { printf("pow 1.5,3\n"); fail++; }
    if (!close_enough(bboeos_pow(2,  10),  pow(2,  10)))  { printf("pow 2,10\n");  fail++; }
    printf("fail=%d\n", fail);
    return fail != 0;
}
"""
    out = _compile_and_run(c_source=src, extra_sources=[])
    assert out.strip().endswith("fail=0"), out


def test_stdio_printf_matches_system() -> None:
    """Verify our snprintf format specifiers match the host snprintf output."""
    src = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
/* Provide stub syscall-surface symbols so stdio.c links without
 * syscall.c.  These shadow the host libc names so stdio.c's calls to
 * write/read/open/close/malloc/free resolve to no-ops. */
static int     bboeos_test_close(int fd) { (void)fd; return 0; }
static void    bboeos_test_free(void *p) { (void)p; }
static void   *bboeos_test_malloc(size_t n) { (void)n; return (void*)0; }
static int     bboeos_test_open(const char *p, int f, ...) { (void)p; (void)f; return -1; }
static ssize_t bboeos_test_read(int fd, void *buf, size_t n) {
    (void)fd; (void)buf; (void)n; return 0;
}
static ssize_t bboeos_test_write(int fd, const void *buf, size_t n) {
    (void)fd; (void)buf; return (ssize_t)n;
}
#define close     bboeos_test_close
#define fclose    bboeos_fclose
#define feof      bboeos_feof
#define ferror    bboeos_ferror
#define fflush    bboeos_fflush
#define fgetc     bboeos_fgetc
#define fopen     bboeos_fopen
#define fprintf   bboeos_fprintf
#define fputc     bboeos_fputc
#define fputs     bboeos_fputs
#define fread     bboeos_fread
#define free      bboeos_test_free
#define fseek     bboeos_fseek
#define ftell     bboeos_ftell
#define fwrite    bboeos_fwrite
#define getchar   bboeos_getchar
#define malloc    bboeos_test_malloc
#define open      bboeos_test_open
#define printf    bboeos_printf
#define putchar   bboeos_putchar
#define puts      bboeos_puts
#define read      bboeos_test_read
#define rewind    bboeos_rewind
#define snprintf  bboeos_snprintf
#define sprintf   bboeos_sprintf
#define stderr    bboeos_stderr
#define stdin     bboeos_stdin
#define stdout    bboeos_stdout
#define vfprintf  bboeos_vfprintf
#define vprintf   bboeos_vprintf
#define vsnprintf bboeos_vsnprintf
#define vsprintf  bboeos_vsprintf
#define write     bboeos_test_write
#include "../tools/libc/stdio.c"
#undef close
#undef fclose
#undef feof
#undef ferror
#undef fflush
#undef fgetc
#undef fopen
#undef fprintf
#undef fputc
#undef fputs
#undef fread
#undef free
#undef fseek
#undef ftell
#undef fwrite
#undef getchar
#undef malloc
#undef open
#undef printf
#undef putchar
#undef puts
#undef read
#undef rewind
#undef snprintf
#undef sprintf
#undef stderr
#undef stdin
#undef stdout
#undef vfprintf
#undef vprintf
#undef vsnprintf
#undef vsprintf
#undef write
#include <stdio.h>

#define CHECK(fmt, ...) do {                                            \
    char ours[128], theirs[128];                                        \
    bboeos_snprintf(ours,   sizeof(ours),   fmt, __VA_ARGS__);          \
    snprintf(       theirs, sizeof(theirs), fmt, __VA_ARGS__);          \
    if (strcmp(ours, theirs) != 0) {                                    \
        printf("DIFF [%s]: ours=[%s] theirs=[%s]\n", fmt, ours, theirs);\
        fail++;                                                         \
    }                                                                   \
} while (0)

int main(void) {
    int fail = 0;
    CHECK("%-10s|",   "hi");
    CHECK("%-5d|",    42);
    CHECK("%05d",     42);
    CHECK("%08x",     0xCAFE);
    CHECK("%10s|",    "hi");
    CHECK("%5d",      42);
    CHECK("%X",       0xBEEF);
    CHECK("%c",       'Z');
    CHECK("%d",       42);
    CHECK("%d %d %s", 1, 2, "three");
    CHECK("%s",       "hello");
    CHECK("%u",       4000000000U);
    CHECK("%x",       0xCAFE);
    printf("fail=%d\n", fail);
    return fail != 0;
}
"""
    out = _compile_and_run(c_source=src, extra_sources=[])
    assert out.strip().endswith("fail=0"), out


def test_stdlib_qsort() -> None:
    """Verify our qsort produces sorted output across edge cases."""
    src = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#define abort    bboeos_abort
#define atexit   bboeos_atexit
#define atoi     bboeos_atoi
#define atol     bboeos_atol
#define bsearch  bboeos_bsearch
#define calloc   bboeos_calloc
#define exit     bboeos_exit
#define free     bboeos_free
#define getenv   bboeos_getenv
#define malloc   bboeos_malloc
#define qsort    bboeos_qsort
#define rand     bboeos_rand
#define realloc  bboeos_realloc
#define srand    bboeos_srand
#define strtol   bboeos_strtol
#define strtoul  bboeos_strtoul
#include "../tools/libc/stdlib.c"
#undef abort
#undef atexit
#undef atoi
#undef atol
#undef bsearch
#undef calloc
#undef exit
#undef free
#undef getenv
#undef malloc
#undef qsort
#undef rand
#undef realloc
#undef srand
#undef strtol
#undef strtoul
#include <stdlib.h>

static int int_cmp(const void *a, const void *b) {
    int x = *(const int*)a, y = *(const int*)b;
    return (x > y) - (x < y);
}

static int str_cmp(const void *a, const void *b) {
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

static int verify_sorted(const int *a, int n, const char *label) {
    for (int i = 1; i < n; i++) {
        if (a[i] < a[i-1]) {
            printf("not sorted [%s] at %d: a[%d]=%d a[%d]=%d\n",
                   label, i, i-1, a[i-1], i, a[i]);
            return 1;
        }
    }
    return 0;
}

int main(void) {
    int fail = 0;

    /* Each scenario is a self-contained block so the list is
     * sortable and individual cases don't share state. */
    {   /* All equal — degenerate pivot case. */
        int a[100];
        for (int i = 0; i < 100; i++) a[i] = 7;
        bboeos_qsort(a, 100, sizeof(int), int_cmp);
        fail += verify_sorted(a, 100, "equal");
    }
    {   /* Already sorted — adversarial for naive pivot. */
        int a[200];
        for (int i = 0; i < 200; i++) a[i] = i;
        bboeos_qsort(a, 200, sizeof(int), int_cmp);
        fail += verify_sorted(a, 200, "sorted");
    }
    {   /* Empty array — no-op. */
        bboeos_qsort(NULL, 0, sizeof(int), int_cmp);
    }
    {   /* Many duplicates. */
        int a[200];
        for (int i = 0; i < 200; i++) a[i] = i % 7;
        bboeos_qsort(a, 200, sizeof(int), int_cmp);
        fail += verify_sorted(a, 200, "dup");
    }
    {   /* Pseudorandom. */
        int a[500];
        unsigned int s = 12345;
        for (int i = 0; i < 500; i++) {
            s = s * 1103515245u + 12345u;
            a[i] = (int)((s >> 16) & 0xFFFF);
        }
        bboeos_qsort(a, 500, sizeof(int), int_cmp);
        fail += verify_sorted(a, 500, "random");
    }
    {   /* Reverse sorted. */
        int a[200];
        for (int i = 0; i < 200; i++) a[i] = 199 - i;
        bboeos_qsort(a, 200, sizeof(int), int_cmp);
        fail += verify_sorted(a, 200, "reverse");
    }
    {   /* Single element. */
        int a[] = {42};
        bboeos_qsort(a, 1, sizeof(int), int_cmp);
        if (a[0] != 42) { printf("single\n"); fail++; }
    }
    {   /* Small distinct — exact-match expected. */
        int a[] = {5, 3, 1, 4, 2};
        int e[] = {1, 2, 3, 4, 5};
        bboeos_qsort(a, 5, sizeof(int), int_cmp);
        if (memcmp(a, e, sizeof(a))) { printf("small\n"); fail++; }
    }
    {   /* String pointers — non-trivial element type. */
        const char *a[] = {"orange", "apple", "kiwi", "banana", "cherry"};
        const char *e[] = {"apple", "banana", "cherry", "kiwi", "orange"};
        bboeos_qsort(a, 5, sizeof(*a), str_cmp);
        for (int i = 0; i < 5; i++) {
            if (strcmp(a[i], e[i])) { printf("strings i=%d\n", i); fail++; }
        }
    }

    printf("fail=%d\n", fail);
    return fail != 0;
}
"""
    out = _compile_and_run(c_source=src, extra_sources=[])
    assert out.strip().endswith("fail=0"), out


def test_string_matches_system() -> None:
    """Verify our string.c agrees with the host libc on representative cases."""
    src = r"""
#include <stdio.h>
#include <string.h>     /* SYSTEM */
#include <strings.h>
#define memchr      bboeos_memchr
#define memcmp      bboeos_memcmp
#define memcpy      bboeos_memcpy
#define memmove     bboeos_memmove
#define memset      bboeos_memset
#define strcasecmp  bboeos_strcasecmp
#define strcat      bboeos_strcat
#define strchr      bboeos_strchr
#define strcmp      bboeos_strcmp
#define strcpy      bboeos_strcpy
#define strdup      bboeos_strdup
#define strerror    bboeos_strerror
#define strlen      bboeos_strlen
#define strncasecmp bboeos_strncasecmp
#define strncat     bboeos_strncat
#define strncmp     bboeos_strncmp
#define strncpy     bboeos_strncpy
#define strrchr     bboeos_strrchr
#define strstr      bboeos_strstr
#include "../tools/libc/string.c"
#undef memchr
#undef memcmp
#undef memcpy
#undef memmove
#undef memset
#undef strcasecmp
#undef strcat
#undef strchr
#undef strcmp
#undef strcpy
#undef strdup
#undef strerror
#undef strlen
#undef strncasecmp
#undef strncat
#undef strncmp
#undef strncpy
#undef strrchr
#undef strstr
#include <string.h>
#include <strings.h>
int main(void) {
    int fail = 0;

    /* Each scenario is a self-contained block so the list is sortable
     * and individual tests don't share state through buffers. */
    {   const char *hello = "hello";
        if (bboeos_memchr(hello, 'l', 5) != (const void*)(hello+2)) { printf("memchr\n"); fail++; }
    }
    {   if (bboeos_memcmp("abc", "abd", 3) >= 0) { printf("memcmp lt\n"); fail++; }
    }
    {   if (bboeos_memcmp("hello", "help", 3) != 0) { printf("memcmp eq\n"); fail++; }
    }
    {   char a[64], b[64];
        for (int i = 0; i < 64; i++) a[i] = i;
        bboeos_memcpy(b, a, 64);
        if (memcmp(a, b, 64)) { printf("memcpy\n"); fail++; }
    }
    {   char a[64], ref[64];
        for (int i = 0; i < 64; i++) { a[i] = i; ref[i] = i; }
        bboeos_memmove(a + 4, a, 32);
        memmove(ref + 4, ref, 32);
        if (memcmp(a, ref, 64)) { printf("memmove\n"); fail++; }
    }
    {   char a[64], ref[64];
        bboeos_memset(a, 0xAB, 64);
        memset(ref, 0xAB, 64);
        if (memcmp(a, ref, 64)) { printf("memset\n"); fail++; }
    }
    {   if (bboeos_strcasecmp("FOO", "foo") != 0) { printf("strcasecmp\n"); fail++; }
    }
    {   char a[64];
        bboeos_strcpy(a, "foo"); bboeos_strcat(a, "bar");
        if (strcmp(a, "foobar")) { printf("strcat\n"); fail++; }
    }
    {   char *foobar = "foobar";
        if (bboeos_strchr(foobar, 'b') != foobar+3) { printf("strchr\n"); fail++; }
    }
    {   if (bboeos_strcmp("a", "b") >= 0) { printf("strcmp lt\n"); fail++; }
    }
    {   char a[64];
        bboeos_strcpy(a, "hello");
        if (strcmp(a, "hello")) { printf("strcpy\n"); fail++; }
    }
    {   if (bboeos_strlen("hello") != 5) { printf("strlen\n"); fail++; }
    }
    {   if (bboeos_strncasecmp("FOObar", "foobaz", 4) != 0) { printf("strncasecmp\n"); fail++; }
    }
    {   if (bboeos_strncmp("hello", "help", 3)) { printf("strncmp eq\n"); fail++; }
    }
    {   char b[8], nref[8] = {0};
        bboeos_strncpy(b, "hi", 8);
        strncpy(nref, "hi", 8);
        if (memcmp(b, nref, 8)) { printf("strncpy\n"); fail++; }
    }
    {   char *foobar = "foobar";
        if (bboeos_strrchr(foobar, 'o') != foobar+2) { printf("strrchr\n"); fail++; }
    }
    {   char *hw = "hello world";
        if (bboeos_strstr(hw, "world") != hw+6) { printf("strstr\n"); fail++; }
    }

    printf("fail=%d\n", fail);
    return fail != 0;
}
"""
    # Pass no extra sources: the test #include's string.c with every
    # public name #define'd to bboeos_*, so the bboeos_* impls are emitted
    # inside the test TU.  Linking string.c separately would also
    # define the un-renamed `memcpy`/`strcpy`/... symbols, overriding
    # the system libc copies and defeating the comparison.
    out = _compile_and_run(c_source=src, extra_sources=[])
    assert out.strip().endswith("fail=0"), out
