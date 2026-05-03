#ifndef BBOEOS_LIBC_LIMITS_H
#define BBOEOS_LIBC_LIMITS_H
/* Standard ISO C limits for the bboeos i386 ABI: char/short/int/long
 * widths.  Doom reads INT_MAX, UINT_MAX, etc. for clamping arithmetic. */

#define CHAR_BIT   8
#define CHAR_MAX   SCHAR_MAX
#define CHAR_MIN   SCHAR_MIN
#define INT_MAX    2147483647
#define INT_MIN    (-2147483647 - 1)
#define LLONG_MAX  9223372036854775807LL
#define LLONG_MIN  (-9223372036854775807LL - 1)
#define LONG_MAX   2147483647L
#define LONG_MIN   (-2147483647L - 1)
#define PATH_MAX   4096
#define SCHAR_MAX  127
#define SCHAR_MIN  (-128)
#define SHRT_MAX   32767
#define SHRT_MIN   (-32768)
#define UCHAR_MAX  255
#define UINT_MAX   4294967295u
#define ULLONG_MAX 18446744073709551615ULL
#define ULONG_MAX  4294967295UL
#define USHRT_MAX  65535

#endif
