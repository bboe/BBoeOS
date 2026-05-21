#ifndef BBOEOS_STDINT_H
#define BBOEOS_STDINT_H

/* Use the compiler's built-in width macros so the underlying type
 * for each fixed-width name stays correct across every context we
 * build for: cc.py (--bits 16 and --bits 32), clang for the bboeos
 * i386 target, and the 64-bit host cc that
 * tests/unit/test_libbboeos.py uses to unit-test the pure functions.
 * See cc/preprocessor.py's _BUILTIN_DEFINES for the cc.py expansions. */
typedef __INT16_TYPE__ int16_t;
typedef __INT32_TYPE__ int32_t;
typedef __INT64_TYPE__ int64_t;
typedef __INT8_TYPE__ int8_t;
typedef int intptr_t;
typedef __UINT16_TYPE__ uint16_t;
typedef __UINT32_TYPE__ uint32_t;
typedef __UINT64_TYPE__ uint64_t;
typedef __UINT8_TYPE__ uint8_t;
typedef unsigned int uintptr_t;

#define INT32_MAX 0x7FFFFFFF
#define INT32_MIN (-INT32_MAX - 1)
#define UINT32_MAX 0xFFFFFFFFu

#endif
