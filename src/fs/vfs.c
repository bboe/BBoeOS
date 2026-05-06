// fs/vfs.c -- Virtual Filesystem Switch
//
// A table of file-scope function_pointer globals dispatches to the
// active filesystem implementation; ``vfs_init`` swaps the pointers
// from bbfs to ext2 if an ext2 superblock is detected.
//
// vfs_init also allocates the FS scratch frame.  Layout (4 KB total,
// ~1.5 KB used):
//
//   0..511     sector_buffer     (512 B; populated by every disk read)
//   512..1535  ext2_sd_buffer    (1 KB; ext2_search_blk's sliding
//                                 2-sector directory window — only
//                                 written on ext2 systems)
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
    uint8_t _opaque[32];
};

// FS scratch frame pointer.  Populated by `vfs_init` from a
// `frame_alloc` + direct-map adjust.  cc.py emits storage as
// `_g_sector_buffer`; the asm `equ` shim aliases the bare name back
// for inline-asm and bbfs.asm / ext2.asm callers.
uint8_t *sector_buffer;
asm("sector_buffer equ _g_sector_buffer");

// ---------------------------------------------------------------------------
// Function-pointer globals.  Each starts pointing at the bbfs
// implementation (the static initialiser uses PR #256's user-function
// constant path); ``vfs_init`` swaps them to ext2 when that filesystem
// is detected.
// ---------------------------------------------------------------------------

int (*vfs_chmod_fn)(uint8_t *path __attribute__((in_register("esi"))),
                    uint8_t mode __attribute__((in_register("ax")))) = bbfs_chmod;
int (*vfs_commit_write_sec_fn)(struct fd *e __attribute__((in_register("esi")))) = bbfs_commit_write_sec;
int (*vfs_create_fn)(uint8_t *path __attribute__((in_register("esi")))) = bbfs_create;
int (*vfs_delete_fn)(uint8_t *path __attribute__((in_register("esi")))) = bbfs_delete;
int (*vfs_find_fn)(uint8_t *path __attribute__((in_register("esi")))) = bbfs_find;
int (*vfs_mkdir_fn)(uint8_t *name __attribute__((in_register("esi")))) = bbfs_mkdir;
int (*vfs_prepare_write_sec_fn)(struct fd *e __attribute__((in_register("esi")))) = bbfs_prepare_write_sec;
int (*vfs_read_dir_fn)(struct fd *e __attribute__((in_register("esi"))),
                       uint8_t *buf __attribute__((in_register("edi")))) = bbfs_read_dir;
int (*vfs_read_sec_fn)(struct fd *e __attribute__((in_register("esi")))) = bbfs_read_sec;
int (*vfs_rename_fn)(uint8_t *old __attribute__((in_register("esi"))),
                     uint8_t *new __attribute__((in_register("edi")))) = bbfs_rename;
int (*vfs_rmdir_fn)(uint8_t *name __attribute__((in_register("esi")))) = bbfs_rmdir;
int (*vfs_update_size_fn)(struct fd *e __attribute__((in_register("esi")))) = bbfs_update_size;

// ---------------------------------------------------------------------------
// vfs_init: allocate FS scratch, detect filesystem, swap function
// pointers if ext2 is present.  ext2_init returns CF clear when the
// superblock magic matches; cc.py translates that into
// ``if (ext2_init())`` evaluating to 1 (true).
// ---------------------------------------------------------------------------

// vfs_init_scratch: allocate the FS scratch frame and store its
// kernel-virt at `sector_buffer`.  Frame_alloc + direct-map adjust;
// pulled out of vfs_init's C body because cc.py doesn't have a
// pointer cast syntax for the int-to-`uint8_t *` conversion.
void vfs_init_scratch();
asm("vfs_init_scratch:\n"
    "        push eax\n"
    "        call frame_alloc\n"
    "        jc .vis_oom\n"
    "        add eax, DIRECT_MAP_BASE\n"
    "        mov [_g_sector_buffer], eax\n"
    "        pop eax\n"
    "        ret\n"
    ".vis_oom:\n"
    // Frame allocator must succeed at boot — the bitmap was just
    // populated from E820 with full conventional + extended RAM free.
    // Hard-stop if it doesn't, rather than silently leaving
    // sector_buffer = NULL.
    "        hlt\n"
    "        jmp .vis_oom\n");

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

__attribute__((carry_return))
int vfs_commit_write_sec(struct fd *e __attribute__((in_register("esi")))) {
    __tail_call(vfs_commit_write_sec_fn, e);
}

__attribute__((carry_return))
int vfs_create(uint8_t *path __attribute__((in_register("esi")))) {
    __tail_call(vfs_create_fn, path);
}

__attribute__((carry_return))
int vfs_delete(uint8_t *path __attribute__((in_register("esi")))) {
    __tail_call(vfs_delete_fn, path);
}

__attribute__((carry_return))
int vfs_find(uint8_t *path __attribute__((in_register("esi")))) {
    __tail_call(vfs_find_fn, path);
}

__attribute__((carry_return))
int vfs_mkdir(uint8_t *name __attribute__((in_register("esi")))) {
    __tail_call(vfs_mkdir_fn, name);
}

__attribute__((carry_return))
int vfs_prepare_write_sec(struct fd *e __attribute__((in_register("esi")))) {
    __tail_call(vfs_prepare_write_sec_fn, e);
}

__attribute__((carry_return))
int vfs_read_dir(struct fd *e __attribute__((in_register("esi"))),
                 uint8_t *buf __attribute__((in_register("edi")))) {
    __tail_call(vfs_read_dir_fn, e, buf);
}

__attribute__((carry_return))
int vfs_read_sec(struct fd *e __attribute__((in_register("esi")))) {
    __tail_call(vfs_read_sec_fn, e);
}

__attribute__((carry_return))
int vfs_rename(uint8_t *old __attribute__((in_register("esi"))),
               uint8_t *new __attribute__((in_register("edi")))) {
    __tail_call(vfs_rename_fn, old, new);
}

__attribute__((carry_return))
int vfs_rmdir(uint8_t *name __attribute__((in_register("esi")))) {
    __tail_call(vfs_rmdir_fn, name);
}

__attribute__((carry_return))
int vfs_update_size(struct fd *e __attribute__((in_register("esi")))) {
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
    "%include \"fs/bbfs.asm\"\n"
    "%include \"fs/ext2.asm\"\n");
