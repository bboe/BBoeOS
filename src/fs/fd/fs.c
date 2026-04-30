// fs/fd/fs.c — read/write implementations for FD_TYPE_DIRECTORY and
// FD_TYPE_FILE.  Dispatched via fd_ops in fs/fd.c when the syscall
// layer hands a directory- or file-typed fd to fd_read / fd_write.
//
// Each function inherits ESI = fd_entry from the fd_read / fd_write
// dispatcher (which tail-jumps after fd_lookup), with EDI = user
// buffer and ECX = byte count from the syscall caller.  Results
// follow cc.py's `carry_return` ABI: AX = bytes copied (or -1 on
// disk error), CF = error flag.

// FS scratch frame pointer — defined in vfs.c, populated by
// `vfs_init` from a `frame_alloc` + direct-map adjust.  Holds
// the kernel-virt of the 4 KB frame whose first 512 bytes back
// the per-sector disk read window.
extern uint8_t *sector_buffer;

// fd entry layout — match the asm-side FD_OFFSET_* offsets.  Only
// `size` and `position` are touched here; the rest is opaque
// padding to keep field offsets aligned with constants.asm.
struct fd {
    uint8_t type;
    uint8_t flags;
    uint16_t start;
    int size;
    int position;
    uint8_t _rest[20];
};

// fs/vfs.asm — runtime function-pointer thunks into the active
// filesystem backend (bbfs / ext2).  All take ESI = fd_entry; the
// sector-cache helpers also return BX = byte offset within the
// freshly cached 512-byte sector_buffer.
__attribute__((carry_return))
int vfs_commit_write_sec(struct fd *entry __attribute__((in_register("esi"))));
__attribute__((carry_return))
int vfs_prepare_write_sec(int *byte_offset __attribute__((out_register("bx"))),
                          struct fd *entry __attribute__((in_register("esi"))));
__attribute__((carry_return))
int vfs_read_dir(int *bytes __attribute__((out_register("ax"))),
                 struct fd *entry __attribute__((in_register("esi"))),
                 uint8_t *buffer __attribute__((in_register("edi"))));
__attribute__((carry_return))
int vfs_read_sec(int *byte_offset __attribute__((out_register("bx"))),
                 struct fd *entry __attribute__((in_register("esi"))));

// fs/fd.c file-scope global; fd_write stashes the user buffer
// pointer here before jumping to this handler.
extern uint8_t *fd_write_buffer;

// In-flight read/write bookkeeping.  Lifted from the original
// fs.asm's three trailing `dd 0` slots.  These are private to this
// translation unit; the asm dispatcher in fd.c never references
// them by name.
struct fd *fd_rw_descriptor_pointer;
int fd_rw_done;
int fd_rw_left;

// fd_read_dir: forward to vfs_read_dir.  The asm version did
// `movsx eax, ax` to sign-extend the 16-bit AX return; cc.py's
// out_register("ax") capture into an `int` slot does the same.
__attribute__((carry_return))
int fd_read_dir(int *result __attribute__((out_register("ax"))),
                struct fd *entry __attribute__((in_register("esi"))),
                uint8_t *buffer __attribute__((in_register("edi")))) {
    int bytes;
    if (!vfs_read_dir(&bytes, entry, buffer)) {
        *result = -1;
        return 0;
    }
    *result = bytes;
    return 1;
}

// fd_read_file: copy at most `count` bytes from the file at
// `entry`'s current position into `destination`.  Bumps
// `entry->position` by the bytes actually copied.  Returns AX =
// bytes copied (0 at EOF), CF set on disk error.
__attribute__((carry_return))
int fd_read_file(int *result __attribute__((out_register("ax"))),
                 struct fd *entry __attribute__((in_register("esi"))),
                 uint8_t *destination __attribute__((in_register("edi"))),
                 int count __attribute__((in_register("ecx")))) {
    int byte_offset;
    int chunk;
    int remaining;
    fd_rw_descriptor_pointer = entry;
    remaining = entry->size - entry->position;
    if (remaining <= 0) {
        *result = 0;
        return 1;
    }
    if (count > remaining) {
        count = remaining;
    }
    fd_rw_left = count;
    fd_rw_done = 0;
    while (fd_rw_left > 0) {
        if (!vfs_read_sec(&byte_offset, fd_rw_descriptor_pointer)) {
            *result = -1;
            return 0;
        }
        chunk = 512 - byte_offset;
        if (chunk > fd_rw_left) {
            chunk = fd_rw_left;
        }
        memcpy(destination + fd_rw_done, sector_buffer + byte_offset, chunk);
        fd_rw_done = fd_rw_done + chunk;
        fd_rw_left = fd_rw_left - chunk;
        fd_rw_descriptor_pointer->position = fd_rw_descriptor_pointer->position + chunk;
    }
    *result = fd_rw_done;
    return 1;
}

// fd_write_file: copy `count` bytes from fd_write_buffer into the
// file at `entry`'s current position via vfs_prepare_write_sec /
// sector_buffer / vfs_commit_write_sec.  Bumps `entry->position`
// by the bytes written.  Returns AX = bytes written, CF set on
// disk error.
__attribute__((carry_return))
int fd_write_file(int *result __attribute__((out_register("ax"))),
                  struct fd *entry __attribute__((in_register("esi"))),
                  int count __attribute__((in_register("ecx")))) {
    int byte_offset;
    int chunk;
    fd_rw_descriptor_pointer = entry;
    fd_rw_left = count;
    fd_rw_done = 0;
    while (fd_rw_left > 0) {
        if (!vfs_prepare_write_sec(&byte_offset, fd_rw_descriptor_pointer)) {
            *result = -1;
            return 0;
        }
        chunk = 512 - byte_offset;
        if (chunk > fd_rw_left) {
            chunk = fd_rw_left;
        }
        memcpy(sector_buffer + byte_offset, fd_write_buffer + fd_rw_done, chunk);
        if (!vfs_commit_write_sec(fd_rw_descriptor_pointer)) {
            *result = -1;
            return 0;
        }
        fd_rw_done = fd_rw_done + chunk;
        fd_rw_left = fd_rw_left - chunk;
        fd_rw_descriptor_pointer->position = fd_rw_descriptor_pointer->position + chunk;
    }
    *result = fd_rw_done;
    return 1;
}
