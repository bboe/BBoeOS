#include <ctype.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>

/* Forward-declare functions earlier alphabetic neighbours call into
 * (strcpy from strcat, strlen from strcat/strncat/strdup).  The header
 * already declares them, but the unit test #defines the public names
 * to bboeos_*-prefixed shadows AFTER its first <string.h> include — so
 * when string.c is then #included for the comparison, the in-scope
 * prototypes still carry the un-renamed names and the callers would
 * otherwise hit implicit-function-declaration errors. */
char  *strcpy(char *dst, const char *src);
size_t strlen(const char *s);

void *memchr(const void *s, int c, size_t n) {
    const unsigned char *p = s;
    while (n--) { if (*p == (unsigned char)c) return (void*)p; p++; }
    return NULL;
}

int memcmp(const void *a, const void *b, size_t n) {
    const unsigned char *p = a, *q = b;
    while (n--) { if (*p != *q) return *p - *q; p++; q++; }
    return 0;
}

void *memcpy(void *dst, const void *src, size_t n) {
    unsigned char *d = dst; const unsigned char *s = src;
    while (n--) *d++ = *s++;
    return dst;
}

void *memmove(void *dst, const void *src, size_t n) {
    unsigned char *d = dst; const unsigned char *s = src;
    if (d < s) {
        while (n--) *d++ = *s++;
    } else {
        d += n; s += n;
        while (n--) *--d = *--s;
    }
    return dst;
}

void *memset(void *dst, int c, size_t n) {
    unsigned char *d = dst;
    while (n--) *d++ = (unsigned char)c;
    return dst;
}

int strcasecmp(const char *a, const char *b) {
    while (*a && tolower((unsigned char)*a) == tolower((unsigned char)*b)) { a++; b++; }
    return tolower((unsigned char)*a) - tolower((unsigned char)*b);
}

char *strcat(char *dst, const char *src)  { strcpy(dst + strlen(dst), src); return dst; }

char *strchr(const char *s, int c) {
    while (*s) { if (*s == (char)c) return (char*)s; s++; }
    return c == 0 ? (char*)s : NULL;
}

int strcmp(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; }
    return (unsigned char)*a - (unsigned char)*b;
}

char *strcpy(char *dst, const char *src)  { char *r = dst; while ((*dst++ = *src++)) {} return r; }

char *strdup(const char *s) {
    size_t n = strlen(s) + 1;
    char *r = malloc(n);
    if (r) memcpy(r, s, n);
    return r;
}

char *strerror(int errnum) {
    /* Cases sorted alphabetically by symbolic name (with 0 first since
     * "Success" has no E* prefix).  Returns string literals — POSIX's
     * char* return type is legacy, callers must not modify the result. */
    switch (errnum) {
        case 0:       return "Success";
        case EACCES:  return "Permission denied";
        case EBADF:   return "Bad file descriptor";
        case EEXIST:  return "File exists";
        case EFAULT:  return "Bad address";
        case EINVAL:  return "Invalid argument";
        case EIO:     return "Input/output error";
        case EISDIR:  return "Is a directory";
        case ENOENT:  return "No such file or directory";
        case ENOMEM:  return "Cannot allocate memory";
        case ENOSPC:  return "No space left on device";
        case ENOSYS:  return "Function not implemented";
        case ENOTDIR: return "Not a directory";
        case EPERM:   return "Operation not permitted";
        case ESPIPE:  return "Illegal seek";
        default:      return "Unknown error";
    }
}

size_t strlen(const char *s) { const char *p = s; while (*p) p++; return p - s; }

int strncasecmp(const char *a, const char *b, size_t n) {
    while (n && *a && tolower((unsigned char)*a) == tolower((unsigned char)*b)) { a++; b++; n--; }
    return n ? tolower((unsigned char)*a) - tolower((unsigned char)*b) : 0;
}

char *strncat(char *dst, const char *src, size_t n) {
    char *p = dst + strlen(dst);
    while (n-- && (*p++ = *src++)) {}
    if (n == (size_t)-1) *p = 0;
    return dst;
}

int strncmp(const char *a, const char *b, size_t n) {
    while (n && *a && *a == *b) { a++; b++; n--; }
    return n ? (unsigned char)*a - (unsigned char)*b : 0;
}

char *strncpy(char *dst, const char *src, size_t n) {
    char *r = dst;
    while (n && (*dst++ = *src++)) n--;
    while (n--) *dst++ = 0;
    return r;
}

char *strrchr(const char *s, int c) {
    const char *last = NULL;
    while (*s) { if (*s == (char)c) last = s; s++; }
    if (c == 0) return (char*)s;
    return (char*)last;
}

char *strstr(const char *h, const char *n) {
    if (!*n) return (char*)h;
    for (; *h; h++) {
        const char *a = h, *b = n;
        while (*a && *b && *a == *b) { a++; b++; }
        if (!*b) return (char*)h;
    }
    return NULL;
}
