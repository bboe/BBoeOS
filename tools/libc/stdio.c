/* tools/libc/stdio.c */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

struct FILE {
    int  eof;
    int  err;
    int  fd;
};

static FILE _stderr = {0, 0, 2};
static FILE _stdin  = {0, 0, 0};
static FILE _stdout = {0, 0, 1};
FILE *stderr = &_stderr;
FILE *stdin  = &_stdin;
FILE *stdout = &_stdout;

/* ---------- printf core helpers (file-static) ----------
 *
 * struct sink is the polymorphic output target that vsnprintf and the
 * _emit* helpers share — either a fixed-capacity buffer (sprintf /
 * snprintf path) or a writable fd (vfprintf path).  _emit/_emit_pad/
 * _emit_str write through it; _utoa is a standalone integer formatter. */

struct sink {
    char  *buf;       /* sprintf/snprintf target; NULL = write to fd */
    size_t cap;
    int    fd;
    size_t len;
};

static void _emit(struct sink *s, char c) {
    if (s->buf) {
        if (s->len + 1 < s->cap) s->buf[s->len] = c;
        s->len++;
    } else {
        write(s->fd, &c, 1);
        s->len++;
    }
}

static void _emit_pad(struct sink *s, char pad, size_t n) {
    while (n--) _emit(s, pad);
}

static void _emit_str(struct sink *s, const char *p, size_t n) {
    if (s->buf) {
        while (n--) _emit(s, *p++);
    } else if (n > 0) {
        write(s->fd, p, n);
        s->len += n;
    }
}

static int _utoa(unsigned int v, int base, int upper, int min_digits, char *out) {
    /* min_digits implements integer precision: %.<N>d pads with leading
     * zeros so the digit count is at least N (Doom's HU_Init relies on
     * this for STCFN%.3d → "STCFN033" not "STCFN33"). */
    char tmp[16]; int i = 0;
    const char *digits = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    if (v == 0) tmp[i++] = '0';
    else while (v) { tmp[i++] = digits[v % base]; v /= base; }
    while (i < min_digits && i < (int)sizeof(tmp)) tmp[i++] = '0';
    int n = i;
    while (i--) *out++ = tmp[i];
    *out = 0;
    return n;
}

/* ---------- public surface (alphabetical) ----------
 *
 * Forward-declare the functions earlier alphabetic neighbours call into
 * (vfprintf, vsnprintf).  The header already declares them, but the
 * unit test #defines the public names to bboeos_*-prefixed shadows
 * AFTER its first <stdio.h> include — so when stdio.c is then #included
 * for the comparison, the in-scope prototypes still carry the
 * un-renamed names and the callers would otherwise hit
 * implicit-function-declaration errors. */
int vfprintf(FILE *fp, const char *fmt, va_list ap);
int vsnprintf(char *buf, size_t cap, const char *fmt, va_list ap);

int fclose(FILE *fp) {
    int r = close(fp->fd);
    if (fp != stdin && fp != stdout && fp != stderr) free(fp);
    return r;
}

int feof(FILE *fp) { return fp->eof; }
int ferror(FILE *fp) { return fp->err; }
int fflush(FILE *fp) { (void)fp; return 0; }

int fgetc(FILE *fp) {
    unsigned char c;
    if (read(fp->fd, &c, 1) != 1) { fp->eof = 1; return EOF; }
    return c;
}

FILE *fopen(const char *path, const char *mode) {
    int flags = (mode[0] == 'w') ? (O_WRONLY | O_CREAT | O_TRUNC) : O_RDONLY;
    int fd = open(path, flags);
    if (fd < 0) return NULL;
    FILE *fp = malloc(sizeof(FILE));
    if (!fp) { close(fd); return NULL; }
    fp->fd = fd; fp->err = 0; fp->eof = 0;
    return fp;
}

int fprintf(FILE *fp, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    int n = vfprintf(fp, fmt, ap);
    va_end(ap);
    return n;
}

int fputc(int c, FILE *fp) {
    unsigned char ch = (unsigned char)c;
    if (write(fp->fd, &ch, 1) != 1) { fp->err = 1; return EOF; }
    return ch;
}

int fputs(const char *s, FILE *fp) {
    size_t n = strlen(s);
    if (write(fp->fd, s, n) != (ssize_t)n) { fp->err = 1; return EOF; }
    return (int)n;
}

size_t fread(void *buf, size_t size, size_t nmemb, FILE *fp) {
    /* Loop until we hit `total`, EOF, or error.  bboeos's SYS_IO_READ
     * caps each call at AX = uint16, so a 70 KB request comes back as
     * a short read with the high 16 bits truncated; Doom's W_ReadLump
     * (called for sprite lumps that can be 60+ KB) needs every byte. */
    size_t total = size * nmemb;
    size_t done = 0;
    char *cbuf = (char *)buf;
    while (done < total) {
        ssize_t n = read(fp->fd, cbuf + done, total - done);
        if (n < 0) { fp->err = 1; return done / size; }
        if (n == 0) { fp->eof = 1; break; }
        done += n;
    }
    return done / size;
}

int fseek(FILE *fp, long off, int whence) {
    /* lseek returns the new absolute position; fseek discards it and
     * returns 0 on success, -1 on error (per ISO C). */
    return lseek(fp->fd, (off_t)off, whence) == (off_t)-1 ? -1 : 0;
}

long ftell(FILE *fp) {
    /* SEEK_CUR with offset 0 returns the current position without
     * moving it — the standard ftell trick. */
    off_t pos = lseek(fp->fd, 0, SEEK_CUR);
    return pos == (off_t)-1 ? -1L : (long)pos;
}

size_t fwrite(const void *buf, size_t size, size_t nmemb, FILE *fp) {
    ssize_t n = write(fp->fd, buf, size * nmemb);
    if (n < 0) { fp->err = 1; return 0; }
    return (size_t)n / size;
}

int getchar(void) { return fgetc(stdin); }

int printf(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    int n = vfprintf(stdout, fmt, ap);
    va_end(ap);
    return n;
}

int putchar(int c) { return fputc(c, stdout); }

int puts(const char *s) {
    if (fputs(s, stdout) == EOF) return EOF;
    return fputc('\n', stdout) == EOF ? EOF : 0;
}

int remove(const char *path) { (void)path; return -1; }

int rename(const char *old, const char *new) { (void)old; (void)new; return -1; }

void rewind(FILE *fp) { (void)fp; }

int snprintf(char *buf, size_t cap, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    int n = vsnprintf(buf, cap, fmt, ap);
    va_end(ap);
    return n;
}

int sprintf(char *buf, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    int n = vsnprintf(buf, (size_t)-1, fmt, ap);
    va_end(ap);
    return n;
}

int sscanf(const char *buf, const char *fmt, ...) {
    /* Stub: Doom uses sscanf only for parsing its config file and demo
     * headers; returning 0 (no items matched) makes those calls behave
     * as if the input was empty, leaving Doom's hard-coded defaults in
     * place.  Cheap correct-enough behaviour for Phase A. */
    (void)buf; (void)fmt;
    return 0;
}

int vfprintf(FILE *fp, const char *fmt, va_list ap) {
    char buf[1024];
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    int to_write = n < (int)sizeof(buf) ? n : (int)sizeof(buf) - 1;
    write(fp->fd, buf, to_write);
    return n;
}

int vprintf(const char *fmt, va_list ap) { return vfprintf(stdout, fmt, ap); }

int vsnprintf(char *buf, size_t cap, const char *fmt, va_list ap) {
    struct sink s = { .buf = buf, .cap = cap, .fd = -1, .len = 0 };
    while (*fmt) {
        if (*fmt != '%') { _emit(&s, *fmt++); continue; }
        fmt++;
        int alt = 0, left = 0, plus = 0, zero = 0;
        while (*fmt == '#' || *fmt == '+' || *fmt == '-' || *fmt == '0') {
            if      (*fmt == '#') alt = 1;
            else if (*fmt == '+') plus = 1;
            else if (*fmt == '-') left = 1;
            else if (*fmt == '0') zero = 1;
            fmt++;
        }
        int width = 0;
        if (*fmt == '*') { width = va_arg(ap, int); fmt++; }
        else while (*fmt >= '0' && *fmt <= '9') { width = width * 10 + (*fmt++ - '0'); }
        int prec = -1;
        if (*fmt == '.') {
            fmt++;
            prec = 0;
            if (*fmt == '*') { prec = va_arg(ap, int); fmt++; }
            else while (*fmt >= '0' && *fmt <= '9') { prec = prec * 10 + (*fmt++ - '0'); }
        }
        while (*fmt == 'l' || *fmt == 'h' || *fmt == 'z') fmt++;

        char conv = *fmt++;
        char num[16];
        const char *body = num;
        int  body_len = 0;
        char prefix[2] = {0, 0};

        switch (conv) {
            case '%': _emit(&s, '%'); continue;
            case 'X':
                if (alt) { prefix[0] = '0'; prefix[1] = 'X'; }
                body_len = _utoa(va_arg(ap, unsigned int), 16, 1, prec, num);
                break;
            case 'c': { num[0] = (char)va_arg(ap, int); num[1] = 0; body_len = 1; break; }
            case 'd':
            case 'i': {
                int v = va_arg(ap, int);
                if (v < 0) { prefix[0] = '-'; body_len = _utoa((unsigned)(-v), 10, 0, prec, num); }
                else if (plus) { prefix[0] = '+'; body_len = _utoa((unsigned)v, 10, 0, prec, num); }
                else body_len = _utoa((unsigned)v, 10, 0, prec, num);
                break;
            }
            case 'e': case 'f': case 'g': {
                /* Stub: emit "<float>" so we don't crash if Doom calls it. */
                (void)va_arg(ap, double);
                _emit_str(&s, "<float>", 7);
                continue;
            }
            case 'o':
                body_len = _utoa(va_arg(ap, unsigned int), 8, 0, prec, num);
                break;
            case 'p':
                prefix[0] = '0'; prefix[1] = 'x';
                body_len = _utoa(va_arg(ap, unsigned int), 16, 0, 0, num);
                break;
            case 's': {
                const char *p = va_arg(ap, const char *);
                if (!p) p = "(null)";
                int n = (int)strlen(p);
                if (prec >= 0 && n > prec) n = prec;
                int total = n;
                int pad = width > total ? width - total : 0;
                if (!left) _emit_pad(&s, ' ', pad);
                _emit_str(&s, p, n);
                if (left)  _emit_pad(&s, ' ', pad);
                continue;
            }
            case 'u':
                body_len = _utoa(va_arg(ap, unsigned int), 10, 0, prec, num);
                break;
            case 'x':
                if (alt) { prefix[0] = '0'; prefix[1] = 'x'; }
                body_len = _utoa(va_arg(ap, unsigned int), 16, 0, prec, num);
                break;
            default:  _emit(&s, '%'); _emit(&s, conv); continue;
        }

        int prefix_len = (prefix[0] != 0) + (prefix[1] != 0);
        int total = body_len + prefix_len;
        int pad   = width > total ? width - total : 0;
        if (!left && !zero) _emit_pad(&s, ' ', pad);
        if (prefix_len) _emit_str(&s, prefix, prefix_len);
        if (!left &&  zero) _emit_pad(&s, '0', pad);
        _emit_str(&s, body, body_len);
        if ( left)          _emit_pad(&s, ' ', pad);
    }
    if (s.buf && s.cap > 0) s.buf[s.len < s.cap ? s.len : s.cap - 1] = 0;
    return (int)s.len;
}

int vsprintf(char *buf, const char *fmt, va_list ap) { return vsnprintf(buf, (size_t)-1, fmt, ap); }
