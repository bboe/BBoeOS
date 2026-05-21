#include "types.h"

// fs/vfs.c -- Virtual Filesystem Switch
//
// A table of file-scope function_pointer globals dispatches to the
// active filesystem implementation; ``vfs_init`` swaps the pointers
// from bbfs to ext2 if an ext2 superblock is detected.
//
// FS scratch layout (no longer shares a single frame):
//
//   sector_buffer      — 512 B in kernel .bss (sector_buffer_storage).
//                        Always available; no frame_alloc.  `vfs_init`
//                        publishes the address to consumers by writing
//                        sector_buffer_storage into the pointer cell.
//   ext2_sd_buffer     — 1 KB used inside a 4 KB frame_alloc'd by
//                        `ext2_init` only when the ext2 superblock
//                        magic matches.  bbfs systems never pay for it.
//
// Both are referenced from asm under their bare names.  The asm
// `equ` shims below alias `_g_<name>` (cc.py's storage prefix) back
// to the bare label so existing ``[sector_buffer + offset]`` sites
// in bbfs.asm / ext2.asm read through the runtime pointer cell
// after manual conversion to a ``mov reg, [sector_buffer]; mov X,
// [reg + offset]`` two-instruction sequence.
//
// Each public ``vfs_X`` is a carry_return tail-call that forwards
// to its ``vfs_X_fn`` global.  cc.py emits the load + frame teardown
// + ``jmp eax`` so AX/CF flow back to the original caller unchanged.
//
// vfs_found_* state and the asm filesystem implementations stay in
// the trailing asm() block: bbfs.asm and ext2.asm reference these
// symbols by their bare names (``vfs_found_size``, etc.) and we'd
// have to rewrite both files to use the C-side ``_g_<name>``
// prefix.  Easier to keep them as raw asm storage.

// ---------------------------------------------------------------------------
// bbfs / ext2 entry points (asm side; forward-declared so cc.py treats
// them as user functions whose addresses can be taken).
// ---------------------------------------------------------------------------

__attribute__((carry_return)) int bbfs_chmod();
__attribute__((carry_return)) int bbfs_commit_write_sec();
__attribute__((carry_return)) int bbfs_create();
__attribute__((carry_return)) int bbfs_delete();
__attribute__((carry_return)) int bbfs_find();
__attribute__((carry_return)) int bbfs_init();
__attribute__((carry_return)) int bbfs_mkdir();
__attribute__((carry_return)) int bbfs_prepare_write_sec();
__attribute__((carry_return)) int bbfs_read_dir();
__attribute__((carry_return)) int bbfs_read_sec();
__attribute__((carry_return)) int bbfs_rename();
__attribute__((carry_return)) int bbfs_rmdir();
__attribute__((carry_return)) int bbfs_update_size();

__attribute__((carry_return)) int ext2_chmod();
__attribute__((carry_return)) int ext2_commit_write_sec();
__attribute__((carry_return)) int ext2_create();
__attribute__((carry_return)) int ext2_delete();
__attribute__((carry_return)) int ext2_find();
__attribute__((carry_return)) int ext2_init();
__attribute__((carry_return)) int ext2_mkdir();
__attribute__((carry_return)) int ext2_prepare_write_sec();
__attribute__((carry_return)) int ext2_read_dir();
__attribute__((carry_return)) int ext2_read_sec();
__attribute__((carry_return)) int ext2_rename();
__attribute__((carry_return)) int ext2_rmdir();
__attribute__((carry_return)) int ext2_update_size();

// fd entry layout — only struct-pointer type identity matters for
// the function_pointer signatures below; nothing reads fields here.
struct fd {
    u8 _opaque[32];
};

// FS scratch frame pointer.  Populated by `vfs_init` from a
// `frame_alloc` + direct-map adjust.  cc.py emits storage as
// `_g_sector_buffer`; the asm `equ` shim aliases the bare name back
// for inline-asm and bbfs.asm / ext2.asm callers.
u8 *sector_buffer;
asm("sector_buffer equ _g_sector_buffer");

// ---------------------------------------------------------------------------
// Function-pointer globals.  Each starts pointing at the bbfs
// implementation (the static initialiser uses PR #256's user-function
// constant path); ``vfs_init`` swaps them to ext2 when that filesystem
// is detected.
// ---------------------------------------------------------------------------

int (*vfs_chmod_fn)(u8 *path __attribute__((in_register("esi"))),
                    u8 mode __attribute__((in_register("ax")))) = bbfs_chmod;
int (*vfs_commit_write_sec_fn)(
    struct fd *e __attribute__((in_register("esi")))) = bbfs_commit_write_sec;
int (*vfs_create_fn)(u8 *path
                     __attribute__((in_register("esi")))) = bbfs_create;
int (*vfs_delete_fn)(u8 *path
                     __attribute__((in_register("esi")))) = bbfs_delete;
int (*vfs_find_fn)(u8 *path __attribute__((in_register("esi")))) = bbfs_find;
int (*vfs_mkdir_fn)(u8 *name __attribute__((in_register("esi")))) = bbfs_mkdir;
int (*vfs_prepare_write_sec_fn)(
    struct fd *e __attribute__((in_register("esi")))) = bbfs_prepare_write_sec;
int (*vfs_read_dir_fn)(struct fd *e
                       __attribute__((in_register("esi")))) = bbfs_read_dir;
int (*vfs_read_sec_fn)(struct fd *e
                       __attribute__((in_register("esi")))) = bbfs_read_sec;
int (*vfs_rename_fn)(u8 *old __attribute__((in_register("esi"))),
                     u8 *new __attribute__((in_register("edi")))) = bbfs_rename;
int (*vfs_rmdir_fn)(u8 *name __attribute__((in_register("esi")))) = bbfs_rmdir;
int (*vfs_update_size_fn)(struct fd *e __attribute__((in_register("esi")))) =
    bbfs_update_size;

// ---------------------------------------------------------------------------
// vfs_init: allocate FS scratch, detect filesystem, swap function
// pointers if ext2 is present.  ext2_init returns CF clear when the
// superblock magic matches; cc.py translates that into
// ``if (ext2_init())`` evaluating to 1 (true).
// ---------------------------------------------------------------------------

// vfs_init_scratch: publish the kernel-virt of sector_buffer_storage
// (a 512 B .bss reservation in kernel.asm) into `_g_sector_buffer`.
// No frame_alloc — the storage is statically reserved at link time;
// the bare label `sector_buffer_storage` resolves to its kernel-virt
// in the .bss nobits section, which `high_entry` has already zeroed.
void vfs_init_scratch();
asm("vfs_init_scratch:\n"
    "        mov dword [_g_sector_buffer], sector_buffer_storage\n"
    "        ret\n");

void sector_cache_init();

void vfs_init() {
    vfs_init_scratch();
    sector_cache_init();
    if (ext2_init()) {
        vfs_chmod_fn = ext2_chmod;
        vfs_commit_write_sec_fn = ext2_commit_write_sec;
        vfs_create_fn = ext2_create;
        vfs_delete_fn = ext2_delete;
        vfs_find_fn = ext2_find;
        vfs_mkdir_fn = ext2_mkdir;
        vfs_prepare_write_sec_fn = ext2_prepare_write_sec;
        vfs_read_dir_fn = ext2_read_dir;
        vfs_read_sec_fn = ext2_read_sec;
        vfs_rename_fn = ext2_rename;
        vfs_rmdir_fn = ext2_rmdir;
        vfs_update_size_fn = ext2_update_size;
    } else {
        bbfs_init();
    }
}

// ---------------------------------------------------------------------------
// Public thunks.  Each is a carry_return tail-call through its
// matching ``vfs_X_fn`` global; cc.py emits the frame teardown +
// ``jmp eax`` so AX/CF flow back unchanged.
// ---------------------------------------------------------------------------

// vfs_chmod: kept as a 1-instruction inline-asm thunk because cc.py's
// __tail_call through a function_pointer global routes through EAX,
// which would clobber AL=mode before the handler reads it.  Same
// AL/EAX collision fd_ioctl hits — fd_ioctl works around it with
// ``pinned_register`` on a function_pointer local, but that
// attribute isn't yet supported on file-scope function_pointer
// globals.  Until it is, keep this thunk in raw asm so AL survives.
__attribute__((carry_return)) int vfs_chmod();
asm("vfs_chmod: jmp dword [_g_vfs_chmod_fn]");

__attribute__((carry_return)) int
vfs_commit_write_sec(struct fd *e __attribute__((in_register("esi")))) {
    __tail_call(vfs_commit_write_sec_fn, e);
}

__attribute__((carry_return)) int
vfs_create(u8 *path __attribute__((in_register("esi")))) {
    __tail_call(vfs_create_fn, path);
}

__attribute__((carry_return)) int
vfs_delete(u8 *path __attribute__((in_register("esi")))) {
    __tail_call(vfs_delete_fn, path);
}

__attribute__((carry_return)) int
vfs_find(u8 *path __attribute__((in_register("esi")))) {
    __tail_call(vfs_find_fn, path);
}

__attribute__((carry_return)) int
vfs_mkdir(u8 *name __attribute__((in_register("esi")))) {
    __tail_call(vfs_mkdir_fn, name);
}

__attribute__((carry_return)) int
vfs_prepare_write_sec(struct fd *e __attribute__((in_register("esi")))) {
    __tail_call(vfs_prepare_write_sec_fn, e);
}

__attribute__((carry_return)) int
vfs_read_dir(struct fd *e __attribute__((in_register("esi")))) {
    __tail_call(vfs_read_dir_fn, e);
}

// Cursor/remaining cells consumed by dir_emit (see the asm block
// below).  Each SYS_IO_GETDENTS syscall stamps these from the
// user-supplied buffer + count before dispatching vfs_read_dir.
u8 *dir_emit_cursor = 0;
int dir_emit_remaining = 0;

__attribute__((carry_return)) int
vfs_read_sec(struct fd *e __attribute__((in_register("esi")))) {
    __tail_call(vfs_read_sec_fn, e);
}

__attribute__((carry_return)) int
vfs_rename(u8 *old __attribute__((in_register("esi"))),
           u8 *new __attribute__((in_register("edi")))) {
    __tail_call(vfs_rename_fn, old, new);
}

__attribute__((carry_return)) int
vfs_rmdir(u8 *name __attribute__((in_register("esi")))) {
    __tail_call(vfs_rmdir_fn, name);
}

__attribute__((carry_return)) int
vfs_update_size(struct fd *e __attribute__((in_register("esi")))) {
    __tail_call(vfs_update_size_fn, e);
}

// ---------------------------------------------------------------------------
// vfs_found_* data + bbfs/ext2 includes.  Stays in raw asm because
// bbfs.asm and ext2.asm reference these labels by their bare names;
// rewriting both implementations to use ``_g_<name>`` would be a much
// bigger change with no real benefit.
// ---------------------------------------------------------------------------

asm("vfs_found_dir_off  dw 0\n"
    "vfs_found_dir_sec  dw 0\n"
    "vfs_found_inode    dw 0\n"
    "vfs_found_mode     db 0\n"
    "vfs_found_size     dd 0\n"
    "vfs_found_type     db 0\n"
    "\n"
    ";; ----------------------------------------------------------------\n"
    ";; dir_emit — kernel-side packer for Linux-style getdents records.\n"
    ";; bbfs_read_dir and ext2_read_dir call this once per live entry.\n"
    ";;\n"
    ";; The caller's user-buffer state lives in two globals set up by\n"
    ";; the SYS_IO_GETDENTS handler before vfs_read_dir is dispatched:\n"
    ";;     dir_emit_cursor    — user-virt write pointer\n"
    ";;     dir_emit_remaining — bytes still free in the user buffer\n"
    ";; Globals avoid passing a context pointer through the fs vtable.\n"
    ";; The kernel is single-threaded for fs ops, so the globals are\n"
    ";; safe for the duration of one getdents syscall.\n"
    ";;\n"
    ";; Record layout (variable length, padded to 4-byte boundary):\n"
    ";;     offset 0  u32 d_ino\n"
    ";;     offset 4  u16 d_reclen\n"
    ";;     offset 6  u8  d_type\n"
    ";;     offset 7  char     d_name[]  (null-terminated, padded)\n"
    ";; reclen = (7 + namelen + 1 + 3) & ~3 = (namelen + 8) & ~3.\n"
    ";;\n"
    ";; Inputs:  AL=d_type, ECX=namelen (excluding NUL), EDX=ino,\n"
    ";;          EDI=name pointer (kernel-readable).\n"
    ";; Output:  CF=0 record written, cursor/remaining advanced.\n"
    ";;          CF=1 buffer too small for record, state unchanged.\n"
    ";; Clobbers EAX, EBX, ECX, EDX, EDI; preserves ESI.\n"
    ";; ----------------------------------------------------------------\n"
    "dir_emit:\n"
    "        push esi\n"
    "        ;; reclen = round_up(7 + namelen + 1, 4) = (namelen + 11) & ~3.\n"
    "        ;; 7 = ino(4) + reclen(2) + type(1); +1 for the NUL terminator;\n"
    "        ;; +3 is the round-up bias before masking to a 4-byte multiple.\n"
    "        mov ebx, ecx\n"
    "        add ebx, 11\n"
    "        and ebx, ~3              ; ebx = reclen\n"
    "        cmp ebx, [_g_dir_emit_remaining]\n"
    "        ja .dir_emit_full\n"
    "        mov esi, edi             ; src = name pointer\n"
    "        mov edi, [_g_dir_emit_cursor]\n"
    "        mov [edi], edx           ; d_ino\n"
    "        mov [edi+4], bx          ; d_reclen\n"
    "        mov [edi+6], al          ; d_type\n"
    "        add edi, 7\n"
    "        cld\n"
    "        rep movsb                ; copy name bytes\n"
    "        mov byte [edi], 0        ; NUL terminator\n"
    "        add [_g_dir_emit_cursor], ebx\n"
    "        sub [_g_dir_emit_remaining], ebx\n"
    "        pop esi\n"
    "        clc\n"
    "        ret\n"
    ".dir_emit_full:\n"
    "        pop esi\n"
    "        stc\n"
    "        ret\n"
    "\n"
    "%include \"fs/bbfs.asm\"\n"
    "%include \"fs/ext2.asm\"\n");
