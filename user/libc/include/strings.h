#ifndef BBOEOS_LIBC_STRINGS_H
#define BBOEOS_LIBC_STRINGS_H
/* BSD strings.h is just the byte-string subset of string.h.  Doom
 * pulls this for strcasecmp / strncasecmp; everything they need is
 * already declared in our <string.h>. */
#include <string.h>
#endif
