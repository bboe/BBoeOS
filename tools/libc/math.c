#include <math.h>

double atan2(double y, double x) {
    double r;
    __asm__("fpatan" : "=t"(r) : "0"(x), "u"(y) : "st(1)");
    return r;
}

float atan2f(float y, float x) { return (float)atan2(y, x); }

double ceil(double x) {
    double r;
    unsigned short cw, cw_ceil;
    __asm__("fnstcw %0" : "=m"(cw));
    cw_ceil = (cw & ~0x0C00) | 0x0800;      /* RC = 10 (round up) */
    __asm__("fldcw %0" : : "m"(cw_ceil));
    __asm__("frndint" : "=t"(r) : "0"(x));
    __asm__("fldcw %0" : : "m"(cw));
    return r;
}

double cos(double x)  { double r; __asm__("fcos"  : "=t"(r) : "0"(x)); return r; }
float  cosf(float x)  { return (float)cos(x); }

double exp(double x) {
    /* exp(x) = 2^(x * log2(e)) using f2xm1 + fscale. */
    double r;
    __asm__ volatile (
        "fldl2e\n\t"
        "fmulp\n\t"
        "fld %%st(0)\n\t"
        "frndint\n\t"
        "fsubr %%st(0), %%st(1)\n\t"
        "fxch\n\t"
        "f2xm1\n\t"
        "fld1\n\t"
        "faddp\n\t"
        "fscale\n\t"
        "fstp %%st(1)\n\t"
        : "=t"(r) : "0"(x));
    return r;
}

double fabs(double x) { double r; __asm__("fabs"  : "=t"(r) : "0"(x)); return r; }
float  fabsf(float x) { return (float)fabs(x); }

double floor(double x) {
    double r;
    unsigned short cw, cw_floor;
    __asm__("fnstcw %0" : "=m"(cw));
    cw_floor = (cw & ~0x0C00) | 0x0400;     /* RC = 01 (round down) */
    __asm__("fldcw %0" : : "m"(cw_floor));
    __asm__("frndint" : "=t"(r) : "0"(x));
    __asm__("fldcw %0" : : "m"(cw));
    return r;
}

float floorf(float x) { return (float)floor(x); }

double log(double x) {
    /* fyl2x: ST1 * log2(ST0) -> ST(0).  Push ln(2) and use fyl2x for ln. */
    double r;
    __asm__("fldln2; fxch; fyl2x" : "=t"(r) : "0"(x));
    return r;
}

double log10(double x) { double r; __asm__("fldlg2; fxch; fyl2x" : "=t"(r) : "0"(x)); return r; }
double log2(double x)  { double r; __asm__("fld1; fxch; fyl2x"   : "=t"(r) : "0"(x)); return r; }

double pow(double x, double y) {
    if (x == 0) return y > 0 ? 0 : 1;
    return exp(y * log(x));
}

float powf(float x, float y)   { return (float)pow(x, y); }

double sin(double x)  { double r; __asm__("fsin"  : "=t"(r) : "0"(x)); return r; }
float  sinf(float x)  { return (float)sin(x); }
double sqrt(double x) { double r; __asm__("fsqrt" : "=t"(r) : "0"(x)); return r; }
float  sqrtf(float x) { return (float)sqrt(x); }

double tan(double x) {
    double r;
    /* fptan pushes ST0=tan, ST1=1.  We want ST0; pop ST(0) discards 1.0. */
    __asm__("fptan; fstp %%st(0)" : "=t"(r) : "0"(x));
    return r;
}
