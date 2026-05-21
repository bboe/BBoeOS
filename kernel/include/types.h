/* kernel/include/types.h — kernel-private fixed-width integer typedefs.
 *
 * BBoeOS adopts the Linux kernel convention: kernel code uses `u8` /
 * `u16` / `u32` / `u64` (and the signed `s8` / ... / `s64` siblings)
 * instead of userspace's `uint8_t` / `int8_t` / ... names.  The
 * visual distinction is the point — when you see `u32` in a source
 * file you know you're inside the kernel; `uint32_t` would suggest
 * userspace code that happens to be linked into the kernel.
 *
 * cc.py used to spell these as built-in keywords (`uint8_t`, etc.)
 * for kernel and userspace alike; that was a non-standard compiler
 * extension that papered over the missing typedef machinery.  Now
 * that cc.py supports `typedef` (PR #453), kernel `.c` files
 * `#include "types.h"` (directly, or transitively via narrow headers
 * like `registers.h` / `program_state.h` / `pipe.h` that already
 * declare u*-typed fields) and userspace pulls in `<stdint.h>` —
 * matching what real C codebases do.
 *
 * The underlying types are chosen for the widest portability across
 * cc.py's --bits modes:
 *   - `u8`  / `s8`  : `unsigned char` / `signed char` (always 8 bits).
 *   - `u16` / `s16` : `unsigned short` / `signed short` (always 16 bits).
 *   - `u32` / `s32` : `unsigned int` / `signed int`.  The kernel only
 *                     ever compiles under --bits 32 (the bootloader
 *                     is hand-written assembly), where `int` is 32
 *                     bits — so this is correct.  `unsigned long`
 *                     would also be 32-bit-safe but cc.py routes it
 *                     through a separate codegen path (DX:AX-pair
 *                     under --bits 16) that doesn't handle every
 *                     shape regular `int` codegen does.
 *   - `u64` / `s64` : `unsigned long long` / `signed long long`.  cc.py
 *                     has no real 64-bit type yet; the alias parses
 *                     (Tier 1 of Phase 6) but only builtins.c instantiates
 *                     it, and that file remains clang-only until int64
 *                     support lands.
 */

#ifndef BBOEOS_KERNEL_TYPES_H
#define BBOEOS_KERNEL_TYPES_H

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long long u64;
typedef signed char s8;
typedef signed short s16;
typedef signed int s32;
typedef signed long long s64;

#endif
