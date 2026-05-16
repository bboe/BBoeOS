/* strtol.h — minimal libc-compatible string-to-int.

   Subset of standard C's strtol:
   - Skips leading whitespace.
   - Optional leading '+' / '-' sign.
   - Decimal digits only; `base` argument must be 10 or 0.
   - No overflow detection (returns the wrap-around value).
   - `endptr` is accepted for libc-signature compatibility but never
     written: cc.py doesn't yet support pointer-to-pointer writes from
     a non-out_register parameter, and every current caller passes
     NULL.  A future cc.py extension can lift this; the call sites
     don't need to change.

   Lives in a header so each program inlines a private copy — same
   pattern as `line_helpers.h`.  When a real libc lands, replace these
   inclusions with an `extern` declaration and the function disappears
   from each program's compiled size. */

#ifndef STRTOL_H
#define STRTOL_H

#include "ctype.h"

int strtol(char *string, char **endptr, int base) {
    if (base != 10 && base != 0) {
        die("strtol: only base 10 supported\n");
    }
    int index = 0;
    while (isspace(string[index])) {
        index += 1;
    }
    int sign = 1;
    if (string[index] == '-') {
        sign = -1;
        index += 1;
    } else if (string[index] == '+') {
        index += 1;
    }
    int value = 0;
    while (string[index] >= '0' && string[index] <= '9') {
        value = value * 10 + (string[index] - '0');
        index += 1;
    }
    return sign * value;
}

#endif
