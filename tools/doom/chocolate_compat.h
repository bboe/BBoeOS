/* Compatibility shim for chocolate-doom sources fetched into
 * third_party/chocolate-doom-opl/ (by tools/fetch_chocolate_opl.sh).
 *
 * The fetched sources are pinned to chocolate-doom 3.1.0
 * (`35fb1372d10756ca27eca05665bd8a7cebc71c05`).  doomgeneric forked an
 * older chocolate-doom 2.x; its `i_sound.h` and supporting headers are
 * close enough to compile the modern OPL stack but don't define a
 * handful of macros / functions / types the newer sources rely on.
 * This header papers over those gaps without modifying either tree.
 *
 * Wired in via `-include tools/doom/chocolate_compat.h` from
 * tools/build_doom.py for the chocolate sources only — the bboeos
 * backend (`i_sound_bboeos.c`, `opl_bboeos.c`) and doomgeneric core
 * compile unaffected.
 *
 * Items provided here:
 *
 *   PACKED_STRUCT(...)         — packed-struct macro (chocolate's
 *                                src/doomtype.h has it; doomgeneric's
 *                                only ships PACKEDATTR).
 *
 *   opl_driver_ver_t (+ enum)  — chocolate i_sound.h splits OPL "driver
 *   I_SetOPLDriverVer            version" out from the engine's
 *                                gameversion enum (`exe_doom_*` in
 *                                d_mode.h).  doomgeneric's i_sound.h
 *                                lacks both; declare them here so
 *                                i_oplmusic.c compiles.
 *
 *   M_fopen / M_remove         — chocolate's m_misc.h wraps fopen /
 *                                remove for Windows path handling.
 *                                doomgeneric drops the wrappers; map
 *                                straight to libc.
 *
 *   music_opl_module rename    — chocolate declares
 *                                  `extern const music_module_t
 *                                       music_opl_module;`
 *                                doomgeneric's i_sound.h has the
 *                                non-const form.  Both are visible
 *                                inside i_oplmusic.c (it includes
 *                                doomgeneric's i_sound.h transitively),
 *                                producing a "redefinition with
 *                                different type" error.  Rename the
 *                                chocolate-side symbol to
 *                                `bboeos_music_opl_module` so the
 *                                doomgeneric decl never collides.  The
 *                                bboeos backend in i_sound_bboeos.c
 *                                references the renamed symbol via
 *                                its own extern decl.
 */
#ifndef BBOEOS_CHOCOLATE_COMPAT_H
#define BBOEOS_CHOCOLATE_COMPAT_H

#include <stdint.h>  /* for the SDL_SwapBE* helpers below */
#include <stdio.h>   /* for fopen / remove that we forward to */
#include <stdlib.h>  /* for realloc that I_Realloc forwards to */

#ifndef PACKED_STRUCT
#if defined(__GNUC__) || defined(__clang__)
#define PACKED_STRUCT(...) struct __VA_ARGS__ __attribute__((packed))
#else
#define PACKED_STRUCT(...) struct __VA_ARGS__
#endif
#endif

/* Chocolate i_sound.h adds an "OPL driver version" enum that selects
 * between the per-game OPL voice-allocation behaviours; doomgeneric's
 * older i_sound.h omits it.  Reproduce verbatim from chocolate's
 * src/i_sound.h so identifiers and ordering match. */
typedef enum {
    opl_doom1_1_666,    /* Doom 1 v1.666                     */
    opl_doom2_1_666,    /* Doom 2 v1.666, Hexen, Heretic     */
    opl_doom_1_9        /* Doom v1.9, Strife                 */
} opl_driver_ver_t;

void I_SetOPLDriverVer(opl_driver_ver_t ver);

/* Chocolate's m_misc.h wraps these for Windows path handling.  On the
 * GCC / clang freestanding build we just forward to libc (libbboeos
 * provides fopen + remove). */
#define M_fopen(filename, mode)  fopen((filename), (mode))
#define M_remove(path)           remove((path))

/* chocolate's i_swap.h pulls SDL_endian.h for the SDL_SwapBE16 /
 * SDL_SwapBE32 byteswap helpers used by midifile.c (Standard MIDI Files
 * are big-endian on disk).  doomgeneric's older i_swap.h doesn't
 * provide them, and we have no SDL.  i386 is little-endian, so the
 * BE-swappers always do an actual byteswap. */
static inline uint16_t SDL_SwapBE16(uint16_t value) {
    return (uint16_t)((value >> 8) | (value << 8));
}

static inline uint32_t SDL_SwapBE32(uint32_t value) {
    return ((value >> 24) & 0x000000FFu)
         | ((value >>  8) & 0x0000FF00u)
         | ((value <<  8) & 0x00FF0000u)
         | ((value << 24) & 0xFF000000u);
}

/* Chocolate's i_system.c provides I_Realloc as realloc + I_Error on
 * failure.  We forward to plain realloc; allocation failure here will
 * simply return NULL, which midifile.c's track-event loader treats as
 * an error anyway. */
static inline void *I_Realloc(void *pointer, size_t size) {
    return realloc(pointer, size);
}

/* Resolve the const-vs-non-const `music_opl_module` collision between
 * chocolate's i_oplmusic.c and doomgeneric's i_sound.h.
 *
 * Strategy: pre-include doomgeneric's i_sound.h here with the offending
 * extern renamed to a sacrificial symbol, so by the time chocolate's
 * i_oplmusic.c later does `#include "i_sound.h"` the include guard is
 * tripped and the doomgeneric decl is skipped.  Chocolate's own
 * `const music_module_t music_opl_module = { ... }` then defines the
 * symbol cleanly.  i_sound_bboeos.c re-extern's the const form for
 * its music_module_t-table forwarding. */
#define music_opl_module __dg_music_opl_module_unused
#include "i_sound.h"
#undef music_opl_module

#endif
