#ifndef BBOEOS_LIBC_ASSERT_H
#define BBOEOS_LIBC_ASSERT_H
/* Minimal assert: prints file:line on failure and aborts.  No NDEBUG
 * gating — Doom's release build leaves NDEBUG undefined and we want
 * the diagnostic output if an assertion ever fires. */
#include <stdio.h>
#include <stdlib.h>

#define assert(expr) \
    ((expr) ? (void)0 : \
     (fprintf(stderr, "assert: %s:%d: %s\n", __FILE__, __LINE__, #expr), abort()))

#endif
