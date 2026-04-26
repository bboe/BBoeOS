// fd.c -- File descriptor table management
//
// fd_alloc:         Find the first free FD slot (AX = fd number, CF if full)
// fd_close:         SYS_IO_CLOSE -- BX=fd; flushes writable files
// fd_fstat:         SYS_IO_FSTAT -- BX=fd; returns AL=mode, CX:DX=size
// fd_init:          Zero the FD table, pre-open fds 0/1/2 as console
// fd_lookup:        Validate fd in BX, return SI = entry pointer (CF if invalid); CX preserved
// fd_open:          SYS_IO_OPEN  -- SI=filename, AL=flags, DL=mode; returns AX=fd
// fd_pos_to_sector: Convert fd_pos to sector + offset (internal helper)
// fd_read:          SYS_IO_READ  -- BX=fd, DI=buffer, CX=count; returns AX=bytes
// fd_write:         SYS_IO_WRITE -- BX=fd, SI=buffer, CX=count; returns AX=bytes

struct fd {
    uint8_t type;
    uint8_t flags;
    uint16_t start;
    uint16_t size_lo;       // FD_OFFSET_SIZE   (low 16 bits of 32-bit size)
    uint16_t size_hi;       // FD_OFFSET_SIZE+2 (high 16 bits)
    uint16_t position_lo;   // FD_OFFSET_POSITION   (low 16 bits of 32-bit position)
    uint16_t position_hi;   // FD_OFFSET_POSITION+2 (high 16 bits)
    uint16_t directory_sector;
    uint16_t directory_offset;
    uint8_t mode;
    uint8_t _reserved[15];
};

struct fd fd_table[FD_MAX] = {
    {FD_TYPE_CONSOLE, O_RDONLY},
    {FD_TYPE_CONSOLE, O_WRONLY},
    {FD_TYPE_CONSOLE, O_WRONLY},
};

// vfs_found_* globals defined in fs/vfs.asm — accessed via asm_name
uint8_t vfs_found_type __attribute__((asm_name("vfs_found_type")));
uint8_t vfs_found_mode __attribute__((asm_name("vfs_found_mode")));
uint16_t vfs_found_inode __attribute__((asm_name("vfs_found_inode")));
uint16_t vfs_found_size_lo __attribute__((asm_name("vfs_found_size")));
uint16_t vfs_found_size_hi __attribute__((asm_name("vfs_found_size+2")));
uint16_t vfs_found_dir_sec __attribute__((asm_name("vfs_found_dir_sec")));
uint16_t vfs_found_dir_off __attribute__((asm_name("vfs_found_dir_off")));

// fd_ioctl_vga: VGA device-control handler (AL=cmd, SI=entry; CF on unsupported cmd)
__attribute__((carry_return)) int fd_ioctl_vga(struct fd *entry __attribute__((in_register("si"))), int ioctl_cmd __attribute__((in_register("ax"))));

// fd_lookup: Validate fd in BX, return SI = entry pointer (CF set if invalid)
__attribute__((carry_return)) __attribute__((preserve_register("cx"))) int fd_lookup(int file_descriptor __attribute__((in_register("bx"))), struct fd *result __attribute__((out_register("si"))));

// vfs_update_size: write fd position back to directory entry as file size (SI = fd entry)
void vfs_update_size(struct fd *entry __attribute__((in_register("si"))));

// vfs_find: look up path in VFS, populates vfs_found_*; CF on not found
__attribute__((carry_return)) int vfs_find(char *path __attribute__((in_register("si"))));

// vfs_create: create file at path in VFS, populates vfs_found_*; CF on error
__attribute__((carry_return)) int vfs_create(char *path __attribute__((in_register("si"))));

// fd_alloc: Find first free FD slot (AX = fd number, SI = entry pointer; CF set if table full)
__attribute__((carry_return)) int fd_alloc(int *file_descriptor __attribute__((out_register("ax"))), struct fd *entry __attribute__((out_register("si")))) {
    int i;
    struct fd *e;
    i = 0;
    while (i < FD_MAX) {
        e = fd_table + i;
        if (e->type == FD_TYPE_FREE) {
            *entry = e;
            *file_descriptor = i;
            return 1;
        }
        i += 1;
    }
    return 0;
}

// fd_close: Close a file descriptor (BX = fd, CF on error)
__attribute__((carry_return)) int fd_close(int file_descriptor __attribute__((in_register("bx")))) {
    struct fd *entry;
    if (!fd_lookup(file_descriptor, &entry)) {
        return 0;
    }
    if (entry->type == FD_TYPE_FILE && (entry->flags & O_WRONLY)) {
        vfs_update_size(entry);
    }
    memset(entry, 0, sizeof(struct fd));
    return 1;
}

// fd_fstat: Get file status (BX=fd; AL=mode, CX:DX=size, CF on error)
__attribute__((carry_return)) int fd_fstat(int file_descriptor __attribute__((in_register("bx"))), int *size_hi __attribute__((out_register("cx"))), int *size_lo __attribute__((out_register("dx"))), int *mode __attribute__((out_register("ax")))) {
    struct fd *entry;
    if (!fd_lookup(file_descriptor, &entry)) { return 0; }
    *size_hi = entry->size_hi;
    *size_lo = entry->size_lo;
    *mode = entry->mode;
    return 1;
}

void fd_init() {}

// fd_ioctl: Device-control dispatch (BX=fd, AL=cmd; CF on error)
__attribute__((carry_return)) int fd_ioctl(int file_descriptor __attribute__((in_register("bx"))), int ioctl_cmd __attribute__((in_register("ax")))) {
    struct fd *entry;
    uint8_t fd_type;
    if (!fd_lookup(file_descriptor, &entry)) { return 0; }
    fd_type = entry->type;
    if (fd_type == FD_TYPE_VGA) { return fd_ioctl_vga(entry, ioctl_cmd); }
    return 0;
}

// fd_lookup: Validate fd in BX, return SI = entry pointer (CF set if invalid)
__attribute__((carry_return)) __attribute__((preserve_register("cx"))) int fd_lookup(int file_descriptor __attribute__((in_register("bx"))), struct fd *result __attribute__((out_register("si")))) {
    struct fd *entry;
    if (file_descriptor >= FD_MAX) {
        return 0;
    }
    entry = fd_table + file_descriptor;
    if (entry->type == FD_TYPE_FREE) {
        return 0;
    }
    *result = entry;
    return 1;
}

// fd_open: Open a file descriptor (SI=path, AX=flags; AX=fd on success, CF on error)
__attribute__((carry_return)) int fd_open(int *result_fd __attribute__((out_register("ax"))), char *path __attribute__((in_register("si"))), int flags_ax __attribute__((in_register("ax")))) {
    uint8_t open_flags;
    int fd_num;
    struct fd *entry;
    open_flags = flags_ax & 0xFF;
    if (fd_open_is_vga(path)) {
        if (!fd_alloc(&fd_num, &entry)) { *result_fd = -1; return 0; }
        entry->type = FD_TYPE_VGA;
        entry->flags = open_flags;
        *result_fd = fd_num;
        return 1;
    }
    if (!vfs_find(path)) {
        if (!(open_flags & O_CREAT)) { *result_fd = -1; return 0; }
        if (!vfs_create(path)) { *result_fd = -1; return 0; }
    }
    if (!fd_alloc(&fd_num, &entry)) { *result_fd = -1; return 0; }
    fd_populate_from_vfs(entry, open_flags);
    *result_fd = fd_num;
    return 1;
}

// fd_get_read_fn: Return the read handler for an fd (SI=entry → AX=fn or 0)
int fd_get_read_fn(struct fd *entry __attribute__((in_register("si"))));

// fd_get_write_fn: Return the write handler for an fd (SI=entry → AX=fn or 0)
int fd_get_write_fn(struct fd *entry __attribute__((in_register("si"))));

// fd_open_is_vga: Test if path equals "/dev/vga" (SI=path; CF clear = match)
__attribute__((carry_return)) int fd_open_is_vga(char *path __attribute__((in_register("si")))) {
    return memcmp(path, "/dev/vga", 9) == 0;
}

// fd_write_buffer: global buffer pointer used by write handlers (defined in asm block)
uint16_t fd_write_buffer __attribute__((asm_name("fd_write_buffer")));

// fd_read: Read from fd (BX=fd, DI=buf, CX=count → AX=bytes, CF on error)
__attribute__((carry_return)) int fd_read(
    int file_descriptor __attribute__((in_register("bx"))),
    uint8_t *buffer __attribute__((in_register("di"))),
    int count __attribute__((in_register("cx"))))
{
    struct fd *entry;
    int (*read_handler)(
        struct fd *e __attribute__((in_register("si"))),
        uint8_t *b __attribute__((in_register("di"))),
        int n __attribute__((in_register("cx"))));
    if (!fd_lookup(file_descriptor, &entry)) { return 0; }
    read_handler = fd_get_read_fn(entry);
    if (read_handler == 0) { return 0; }
    __tail_call(read_handler, entry, buffer, count);
}

// fd_write: Write to fd (BX=fd, SI=buf, CX=count → AX=bytes, CF on error)
__attribute__((carry_return)) int fd_write(
    int file_descriptor __attribute__((in_register("bx"))),
    uint8_t *buffer __attribute__((in_register("si"))),
    int count __attribute__((in_register("cx"))))
{
    struct fd *entry;
    int (*write_handler)(
        struct fd *e __attribute__((in_register("si"))),
        int n __attribute__((in_register("cx"))));
    fd_write_buffer = buffer;
    if (!fd_lookup(file_descriptor, &entry)) { return 0; }
    write_handler = fd_get_write_fn(entry);
    if (write_handler == 0) { return 0; }
    __tail_call(write_handler, entry, count);
}

// fd_populate_from_vfs: Fill fd entry from vfs_found_* globals (SI=entry, AX=open_flags)
void fd_populate_from_vfs(struct fd *entry __attribute__((in_register("si"))), int open_flags __attribute__((in_register("ax")))) {
    uint8_t trunc_flag;
    entry->type = vfs_found_type;
    entry->flags = open_flags;
    entry->mode = vfs_found_mode;
    entry->start = vfs_found_inode;
    entry->size_lo = vfs_found_size_lo;
    entry->size_hi = vfs_found_size_hi;
    entry->position_lo = 0;
    entry->position_hi = 0;
    entry->directory_sector = vfs_found_dir_sec;
    entry->directory_offset = vfs_found_dir_off;
    trunc_flag = open_flags & O_TRUNC;
    if (trunc_flag) {
        entry->size_lo = 0;
        entry->size_hi = 0;
    }
}

// fd_pos_to_sector: Convert fd position to absolute sector + byte offset
//   SI=entry → AX=sector, BX=byte_offset_in_sector
int fd_pos_to_sector(struct fd *entry __attribute__((in_register("si"))), int *byte_offset __attribute__((out_register("bx")))) {
    int position_lo;
    int position_hi;
    int sector;
    position_lo = entry->position_lo;
    position_hi = entry->position_hi;
    sector = (position_hi << 7) | (position_lo >> 9);
    sector = sector + entry->start;
    *byte_offset = position_lo & 0x1FF;
    return sector;
}

asm("

;;; -----------------------------------------------------------------------
;;; fd_get_read_fn: get read handler pointer from fd_ops (SI=entry → AX)
;;; fd_get_write_fn: get write handler pointer from fd_ops (SI=entry → AX)
;;; Operations table: (read_fn, write_fn) indexed by FD_TYPE_*.
;;; fd_write_buffer: global holding the write buffer pointer for handlers.
;;; -----------------------------------------------------------------------
fd_get_read_fn:
        xor bh, bh
        mov bl, [si+FD_OFFSET_TYPE]
        shl bx, 2               ; * 4: each ops entry is two words
        mov ax, [fd_ops+bx]     ; read_fn
        ret

fd_get_write_fn:
        xor bh, bh
        mov bl, [si+FD_OFFSET_TYPE]
        shl bx, 2               ; * 4: each ops entry is two words
        mov ax, [fd_ops+bx+2]   ; write_fn
        ret

        ;; Operations table: (read_fn, write_fn) indexed by FD_TYPE_*
        ;; A zero entry means unsupported for that type.
fd_ops:
        dw 0,               0                 ; FD_TYPE_FREE (0)
        dw fd_read_console, fd_write_console  ; FD_TYPE_CONSOLE (1)
        dw fd_read_dir,     0                 ; FD_TYPE_DIRECTORY (2)
        dw fd_read_file,    fd_write_file     ; FD_TYPE_FILE (3)
        dw 0,               0                 ; FD_TYPE_ICMP (4)
        dw fd_read_net,     fd_write_net      ; FD_TYPE_NET (5)
        dw 0,               0                 ; FD_TYPE_UDP (6)
        dw 0,               0                 ; FD_TYPE_VGA (7)

        fd_write_buffer dw 0

%include \"fs/fd/console.asm\"
%include \"fs/fd/fs.asm\"
%include \"fs/fd/net.asm\"
");
