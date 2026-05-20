/* strtol.h — minimal libc-compatible string-to-int.

   Subset of standard C's strtol:
   - Skips leading whitespace.
   - Optional leading '+' / '-' sign.
   - Decimal digits only; `base` argument must be 10 or 0.
   - No overflow detection (returns the wrap-around value).
   - `endptr` (if non-NULL) receives a pointer to the first character
     past the parsed digits, matching libc's behaviour.

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
    if (endptr != NULL) {
        *endptr = string + index;
    }
    return sign * value;
}

#endif
