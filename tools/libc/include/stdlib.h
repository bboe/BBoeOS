/* tools/libc/include/stdlib.h */
#ifndef BBOEOS_LIBC_STDLIB_H
#define BBOEOS_LIBC_STDLIB_H
#include <stddef.h>

#define EXIT_FAILURE 1
#define EXIT_SUCCESS 0
#define RAND_MAX     0x7FFFFFFF

void   abort(void) __attribute__((noreturn));
int    atexit(void (*fn)(void));
int    atoi(const char *s);
long   atol(const char *s);
void  *bsearch(const void *key, const void *base, size_t nmemb, size_t size, int (*cmp)(const void *, const void *));
void  *calloc(size_t nmemb, size_t size);
void   exit(int status) __attribute__((noreturn));
void   free(void *p);
char  *getenv(const char *name);    /* always returns NULL */
void  *malloc(size_t n);
void   qsort(void *base, size_t nmemb, size_t size, int (*cmp)(const void *, const void *));
int    rand(void);
void  *realloc(void *p, size_t n);
void   srand(unsigned int seed);
long   strtol(const char *s, char **end, int base);
unsigned long strtoul(const char *s, char **end, int base);

#endif
