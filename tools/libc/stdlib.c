#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* ---------- file-static state and helpers ----------
 *
 * Free-list allocator backed by sbrk.  Each block starts with a header
 * carrying its full byte size (header included) and a pointer to the
 * next free block.  The free list is a NULL-terminated singly linked
 * list, sorted by address so release() can detect adjacency with both
 * neighbours in a single pass and merge runs of free space.  Allocator
 * state is just the head of that list.
 *
 * malloc() walks the list first-fit; on miss it asks grow_heap() to
 * extend the program break by at least one POOL_GROW_MIN-sized chunk
 * via sbrk and inserts the new region as a single free block.  Splits
 * leave the tail in the free list with a fresh header; merges sum
 * adjacent ->bytes and unlink the consumed neighbour.
 *
 * Returned payloads are POOL_ALIGN-byte aligned; sizeof(block_header)
 * is itself a POOL_ALIGN multiple so payload alignment falls out for
 * free as long as block headers start on POOL_ALIGN boundaries (which
 * sbrk + page alignment guarantees for the first chunk and the split
 * arithmetic preserves thereafter). */

#define MAX_ATEXIT     8
#define POOL_ALIGN     8u
#define POOL_GROW_MIN  4096u
#define QSORT_CUTOFF   16    /* partitions ≤ CUTOFF finish via insertion sort */

typedef struct block_header block_header;
struct block_header {
    size_t        bytes;          /* full block size, header included */
    block_header *next;           /* next free block, or NULL */
};

static int _atexit_count = 0;
static void (*_atexit_fns[MAX_ATEXIT])(void);
static unsigned int _rand_state = 1;
static block_header *free_list = NULL;

/* Forward-declare static helpers so each one can sit in alphabetical
 * order without worrying about who calls whom (grow_heap → release). */
static void   _swap(unsigned char *a, unsigned char *b, size_t size);
static size_t align_up(size_t value, size_t alignment);
static int    grow_heap(size_t need_bytes);
static void   release(block_header *block);

/* Forward-declare public functions earlier alphabetic neighbours call
 * into (atoi/atol → strtol; calloc → malloc).  The header already
 * declares them, but the unit test #defines the public names to
 * bboeos_*-prefixed shadows AFTER its first <stdlib.h> include — so
 * when stdlib.c is then #included for the comparison, the in-scope
 * prototypes still carry the un-renamed names and the callers would
 * otherwise hit implicit-function-declaration errors. */
void *malloc(size_t bytes);
long  strtol(const char *s, char **end, int base);

static void _swap(unsigned char *a, unsigned char *b, size_t size) {
    while (size--) { unsigned char t = *a; *a++ = *b; *b++ = t; }
}

static size_t align_up(size_t value, size_t alignment) {
    return (value + alignment - 1) & ~(alignment - 1);
}

static int grow_heap(size_t need_bytes) {
    size_t request = align_up(need_bytes, POOL_GROW_MIN);
    void *base = sbrk((ptrdiff_t)request);
    if (base == (void *)-1) return 0;
    block_header *fresh = (block_header *)base;
    fresh->bytes = request;
    fresh->next = NULL;
    release(fresh);
    return 1;
}

static void release(block_header *block) {
    /* Locate the insertion point: prev/curr straddle the address we're
     * inserting, with prev < block <= curr (or NULL on either end). */
    block_header *prev = NULL;
    block_header *curr = free_list;
    while (curr && curr < block) {
        prev = curr;
        curr = curr->next;
    }
    /* Coalesce upward (block adjoins curr). */
    if (curr && (char *)block + block->bytes == (char *)curr) {
        block->bytes += curr->bytes;
        block->next = curr->next;
    } else {
        block->next = curr;
    }
    /* Coalesce downward (prev adjoins block). */
    if (prev && (char *)prev + prev->bytes == (char *)block) {
        prev->bytes += block->bytes;
        prev->next = block->next;
    } else if (prev) {
        prev->next = block;
    } else {
        free_list = block;
    }
}

/* ---------- public surface (alphabetical) ---------- */

void abort(void) { _exit(134); }    /* 128 + SIGABRT(6) */

int atexit(void (*fn)(void)) {
    if (_atexit_count >= MAX_ATEXIT) return -1;
    _atexit_fns[_atexit_count++] = fn;
    return 0;
}

int  atoi(const char *s) { return (int)strtol(s, NULL, 10); }
long atol(const char *s) { return strtol(s, NULL, 10); }

void *bsearch(const void *key, const void *base, size_t n, size_t size, int (*cmp)(const void *, const void *)) {
    const unsigned char *a = base;
    size_t lo = 0, hi = n;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        int c = cmp(key, a + mid * size);
        if      (c < 0) hi = mid;
        else if (c > 0) lo = mid + 1;
        else return (void *)(a + mid * size);
    }
    return NULL;
}

void *calloc(size_t nmemb, size_t size) {
    size_t bytes = nmemb * size;
    void *p = malloc(bytes);
    if (p) memset(p, 0, bytes);
    return p;
}

void exit(int status) {
    while (_atexit_count > 0) _atexit_fns[--_atexit_count]();
    _exit(status);
}

void free(void *payload) {
    if (!payload) return;
    block_header *block = (block_header *)((char *)payload - sizeof(block_header));
    release(block);
}

char *getenv(const char *name) { (void)name; return NULL; }

void *malloc(size_t bytes) {
    if (bytes == 0) return NULL;
    size_t total = align_up(bytes + sizeof(block_header), POOL_ALIGN);
    block_header *prev;
    block_header *curr;
    for (;;) {
        prev = NULL;
        curr = free_list;
        while (curr && curr->bytes < total) {
            prev = curr;
            curr = curr->next;
        }
        if (curr) break;
        if (!grow_heap(total)) return NULL;
    }
    /* Split if there's room for another block (header + at least one
     * payload alignment unit); otherwise hand out the whole block. */
    if (curr->bytes >= total + sizeof(block_header) + POOL_ALIGN) {
        block_header *tail = (block_header *)((char *)curr + total);
        tail->bytes = curr->bytes - total;
        tail->next = curr->next;
        curr->bytes = total;
        if (prev) prev->next = tail;
        else      free_list = tail;
    } else {
        if (prev) prev->next = curr->next;
        else      free_list = curr->next;
    }
    return (char *)curr + sizeof(block_header);
}

void qsort(void *base, size_t n, size_t size, int (*cmp)(const void *, const void *)) {
    /* Sedgewick-style quicksort.  Per partition:
     *   - Median-of-three picks the pivot from {first, middle, last}
     *     and sorts the three so a[first] ≤ pivot ≤ a[last].  Those
     *     endpoints then act as sentinels so the inner scan loops can
     *     skip explicit bounds checks.
     *   - The pivot lives at a[n-2] during partition; the i/j scans
     *     start at the ends and crawl inward, swapping pairs that are
     *     on the wrong side.  Once they cross, the pivot is swapped
     *     into its final resting position.
     *   - Partitions ≤ QSORT_CUTOFF skip recursion entirely and are
     *     mopped up by an insertion-sort pass at the bottom.
     *   - We recurse on the smaller half and iterate on the larger,
     *     bounding the call stack at O(log n) regardless of input. */

    unsigned char *a = base;
    while (n > QSORT_CUTOFF) {
        unsigned char *lo  = a;
        unsigned char *mid = a + (n / 2) * size;
        unsigned char *hi  = a + (n - 1) * size;

        if (cmp(lo,  mid) > 0) _swap(lo,  mid, size);
        if (cmp(lo,  hi)  > 0) _swap(lo,  hi,  size);
        if (cmp(mid, hi)  > 0) _swap(mid, hi,  size);

        unsigned char *pivot = hi - size;
        _swap(mid, pivot, size);

        size_t i = 0, j = n - 2;
        for (;;) {
            do { i++; } while (cmp(a + i*size, pivot) < 0);
            do { j--; } while (cmp(a + j*size, pivot) > 0);
            if (i >= j) break;
            _swap(a + i*size, a + j*size, size);
        }
        _swap(a + i*size, pivot, size);

        size_t left  = i;
        size_t right = n - i - 1;
        if (left < right) {
            qsort(a, left, size, cmp);
            a += (i + 1) * size;
            n  = right;
        } else {
            qsort(a + (i + 1)*size, right, size, cmp);
            n  = left;
        }
    }

    /* Insertion-sort tail: handles small initial inputs and finishes
     * each partition once it drops below QSORT_CUTOFF. */
    for (size_t i = 1; i < n; i++) {
        for (size_t j = i; j > 0 && cmp(a + (j - 1) * size, a + j * size) > 0; j--) {
            _swap(a + (j - 1) * size, a + j * size, size);
        }
    }
}

int rand(void) {
    _rand_state = _rand_state * 1103515245u + 12345u;
    return (int)((_rand_state >> 16) & 0x7FFFFFFF);
}

void *realloc(void *payload, size_t bytes) {
    if (!payload) return malloc(bytes);
    if (bytes == 0) { free(payload); return NULL; }
    block_header *block = (block_header *)((char *)payload - sizeof(block_header));
    size_t old_payload = block->bytes - sizeof(block_header);
    if (bytes <= old_payload) return payload;
    void *fresh = malloc(bytes);
    if (fresh) {
        memcpy(fresh, payload, old_payload);
        free(payload);
    }
    return fresh;
}

void srand(unsigned int seed) { _rand_state = seed; }

long strtol(const char *s, char **end, int base) {
    while (isspace((unsigned char)*s)) s++;
    int neg = 0;
    if (*s == '+' || *s == '-') { neg = (*s == '-'); s++; }
    if ((base == 0 || base == 16) && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) { s += 2; base = 16; }
    else if (base == 0 && s[0] == '0') { s++; base = 8; }
    else if (base == 0) base = 10;
    long acc = 0;
    while (*s) {
        int d;
        if (*s >= '0' && *s <= '9') d = *s - '0';
        else if (*s >= 'a' && *s <= 'z') d = *s - 'a' + 10;
        else if (*s >= 'A' && *s <= 'Z') d = *s - 'A' + 10;
        else break;
        if (d >= base) break;
        acc = acc * base + d;
        s++;
    }
    if (end) *end = (char *)s;
    return neg ? -acc : acc;
}

unsigned long strtoul(const char *s, char **end, int base) { return (unsigned long)strtol(s, end, base); }
