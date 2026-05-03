#include "ctype.h"

/* Class bits.  Values are arbitrary powers of two (the table and the
 * is*() functions reference these symbolically); ordered alphabetically
 * by name with sequential bit positions to match. */
#define _B  0x01    /* blank (space, tab) — not exposed but reusable */
#define _C  0x02    /* control */
#define _D  0x04    /* digit */
#define _L  0x08    /* lower */
#define _P  0x10    /* punct */
#define _S  0x20    /* space */
#define _U  0x40    /* upper */
#define _X  0x80    /* hex digit */

static const unsigned char _ctype[256] = {
    /* 0x00-0x1F: control */
    _C,_C,_C,_C,_C,_C,_C,_C, _C,_C|_S,_C|_S,_C|_S,_C|_S,_C|_S,_C,_C,
    _C,_C,_C,_C,_C,_C,_C,_C, _C,_C,_C,_C,_C,_C,_C,_C,
    /* 0x20 SP, 0x21-0x2F punct */
    _S|_B,_P,_P,_P,_P,_P,_P,_P, _P,_P,_P,_P,_P,_P,_P,_P,
    /* 0x30-0x39 digits, 0x3A-0x3F punct */
    _D|_X,_D|_X,_D|_X,_D|_X,_D|_X,_D|_X,_D|_X,_D|_X, _D|_X,_D|_X,_P,_P,_P,_P,_P,_P,
    /* 0x40 punct, 0x41-0x46 upper-hex, 0x47-0x5A upper, 0x5B-0x60 punct */
    _P,_U|_X,_U|_X,_U|_X,_U|_X,_U|_X,_U|_X,_U, _U,_U,_U,_U,_U,_U,_U,_U,
    _U,_U,_U,_U,_U,_U,_U,_U, _U,_U,_U,_P,_P,_P,_P,_P,
    /* 0x60 punct, 0x61-0x66 lower-hex, 0x67-0x7A lower, 0x7B-0x7E punct, 0x7F ctrl */
    _P,_L|_X,_L|_X,_L|_X,_L|_X,_L|_X,_L|_X,_L, _L,_L,_L,_L,_L,_L,_L,_L,
    _L,_L,_L,_L,_L,_L,_L,_L, _L,_L,_L,_P,_P,_P,_P,_C,
    /* 0x80-0xFF: zero — non-ASCII unclassified */
};

int isalnum(int c)  { return (unsigned)c < 256 && (_ctype[c] & (_U|_L|_D)); }
int isalpha(int c)  { return (unsigned)c < 256 && (_ctype[c] & (_U|_L)); }
int iscntrl(int c)  { return (unsigned)c < 256 && (_ctype[c] &  _C); }
int isdigit(int c)  { return (unsigned)c < 256 && (_ctype[c] &  _D); }
int islower(int c)  { return (unsigned)c < 256 && (_ctype[c] &  _L); }
int isprint(int c)  { return (unsigned)c < 256 && !(_ctype[c] & _C); }
int ispunct(int c)  { return (unsigned)c < 256 && (_ctype[c] &  _P); }
int isspace(int c)  { return (unsigned)c < 256 && (_ctype[c] &  _S); }
int isupper(int c)  { return (unsigned)c < 256 && (_ctype[c] &  _U); }
int isxdigit(int c) { return (unsigned)c < 256 && (_ctype[c] &  _X); }
int tolower(int c)  { return (c >= 'A' && c <= 'Z') ? c + 32 : c; }
int toupper(int c)  { return (c >= 'a' && c <= 'z') ? c - 32 : c; }
