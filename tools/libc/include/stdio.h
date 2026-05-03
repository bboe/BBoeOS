#ifndef BBOEOS_LIBC_STDIO_H
#define BBOEOS_LIBC_STDIO_H
#include <stdarg.h>
#include <stddef.h>

typedef struct FILE FILE;
extern FILE *stderr, *stdin, *stdout;

#define BUFSIZ 1024
#define EOF    (-1)

int    fclose(FILE *fp);
int    feof(FILE *fp);
int    ferror(FILE *fp);
int    fflush(FILE *fp);
int    fgetc(FILE *fp);
FILE  *fopen(const char *path, const char *mode);
int    fprintf(FILE *fp, const char *fmt, ...);
int    fputc(int c, FILE *fp);
int    fputs(const char *s, FILE *fp);
size_t fread(void *buf, size_t size, size_t nmemb, FILE *fp);
int    fseek(FILE *fp, long off, int whence);
long   ftell(FILE *fp);
size_t fwrite(const void *buf, size_t size, size_t nmemb, FILE *fp);
int    getchar(void);
int    printf(const char *fmt, ...);
int    putchar(int c);
int    puts(const char *s);
void   rewind(FILE *fp);
int    snprintf(char *buf, size_t n, const char *fmt, ...);
int    sprintf(char *buf, const char *fmt, ...);
int    vfprintf(FILE *fp, const char *fmt, va_list ap);
int    vprintf(const char *fmt, va_list ap);
int    vsnprintf(char *buf, size_t n, const char *fmt, va_list ap);
int    vsprintf(char *buf, const char *fmt, va_list ap);

#endif
