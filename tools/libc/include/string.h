#ifndef BBOEOS_LIBC_STRING_H
#define BBOEOS_LIBC_STRING_H
#include <stddef.h>

void  *memchr(const void *s, int c, size_t n);
int    memcmp(const void *a, const void *b, size_t n);
void  *memcpy(void *dst, const void *src, size_t n);
void  *memmove(void *dst, const void *src, size_t n);
void  *memset(void *dst, int c, size_t n);
int    strcasecmp(const char *a, const char *b);
char  *strcat(char *dst, const char *src);
char  *strchr(const char *s, int c);
int    strcmp(const char *a, const char *b);
char  *strcpy(char *dst, const char *src);
char  *strdup(const char *s);
char  *strerror(int errnum);
size_t strlen(const char *s);
int    strncasecmp(const char *a, const char *b, size_t n);
char  *strncat(char *dst, const char *src, size_t n);
int    strncmp(const char *a, const char *b, size_t n);
char  *strncpy(char *dst, const char *src, size_t n);
char  *strrchr(const char *s, int c);
char  *strstr(const char *haystack, const char *needle);

#endif
