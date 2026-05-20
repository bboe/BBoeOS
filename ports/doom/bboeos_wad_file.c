/* tools/doom/bboeos_wad_file.c — slurp-on-open WAD backend.
 *
 * doomgeneric's w_file.h defines wad_file_class_t, a virtual class
 * with OpenFile / CloseFile / Read methods + a mapped-pointer flag.
 * If a backend sets wad_file_t::mapped to non-NULL, W_CacheLumpNum
 * short-circuits every lump lookup to (mapped + lump->position) —
 * no fread, no fseek, no syscall for the rest of the session.  The
 * upstream `posix_wad_file` exploits this with mmap; we don't have
 * mmap on bboeos, but we don't actually need lazy faulting — Doom
 * touches most of the WAD over a session and the WAD is read-only.
 * So this file provides a `stdc_wad_file` symbol — the default
 * backend that doomgeneric falls through to without `-mmap` —
 * that reads the entire WAD into a malloc'd buffer at OpenFile and
 * exposes that buffer via wad_file_t::mapped.
 *
 * Trades one big fread (≈ 1 s for shareware doom1.wad on QEMU) at
 * boot for hundreds of fseek + small-fread pairs scattered across
 * every level load.  P_SetupLevel's WAD I/O collapses to memcpy
 * from the slurp buffer.
 *
 * tools/build_doom.py adds "w_file_stdc" to its excluded source
 * list so doomgeneric's stock W_StdC_* implementations don't fight
 * this one at link time. */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "doomtype.h"
#include "w_file.h"

typedef struct {
    wad_file_t wad;
    /* No additional state — wad.mapped + wad.length is everything
     * the caller needs.  We don't keep the FILE* around because the
     * file is fully consumed at OpenFile time. */
} bboeos_wad_file_t;

extern wad_file_class_t stdc_wad_file;

static void W_BBoeOS_CloseFile(wad_file_t *wad) {
    bboeos_wad_file_t *self = (bboeos_wad_file_t *)wad;
    free(self->wad.mapped);
    free(self);
}

static wad_file_t *W_BBoeOS_OpenFile(char *path) {
    size_t bytes_read;
    FILE *fstream;
    long length;
    bboeos_wad_file_t *result;

    fstream = fopen(path, "rb");
    if (fstream == NULL) {
        return NULL;
    }
    if (fseek(fstream, 0, SEEK_END) != 0) {
        fclose(fstream);
        return NULL;
    }
    length = ftell(fstream);
    if (length < 0) {
        fclose(fstream);
        return NULL;
    }
    if (fseek(fstream, 0, SEEK_SET) != 0) {
        fclose(fstream);
        return NULL;
    }

    /* malloc rather than Z_Malloc throughout: Doom's zone is sized
     * for gameplay allocations, not a 4 MB durable WAD buffer, and
     * mixing the two allocators on a single open/close lifecycle
     * adds nothing.  malloc routes to libbboeos's sbrk-backed
     * allocator, which has the full user heap to work with. */
    result = malloc(sizeof(bboeos_wad_file_t));
    if (result == NULL) {
        fclose(fstream);
        return NULL;
    }
    result->wad.file_class = &stdc_wad_file;
    result->wad.length = (unsigned int)length;
    result->wad.mapped = malloc((size_t)length);
    if (result->wad.mapped == NULL) {
        fclose(fstream);
        free(result);
        return NULL;
    }
    bytes_read = fread(result->wad.mapped, 1, (size_t)length, fstream);
    fclose(fstream);
    if (bytes_read != (size_t)length) {
        free(result->wad.mapped);
        free(result);
        return NULL;
    }
    return &result->wad;
}

static size_t W_BBoeOS_Read(wad_file_t *wad, unsigned int offset,
                            void *buffer, size_t buffer_len) {
    /* W_CacheLumpNum short-circuits to (mapped + position) when
     * mapped != NULL, so this entry point is rarely hit — only
     * W_AddFile reads the WAD header + directory through it during
     * startup.  Just memcpy from the slurped buffer. */
    if (offset >= wad->length) {
        return 0;
    }
    if (offset + buffer_len > wad->length) {
        buffer_len = wad->length - offset;
    }
    memcpy(buffer, wad->mapped + offset, buffer_len);
    return buffer_len;
}

wad_file_class_t stdc_wad_file = {
    W_BBoeOS_OpenFile,
    W_BBoeOS_CloseFile,
    W_BBoeOS_Read,
};
