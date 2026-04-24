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

// fd_lookup: Validate fd in BX, return SI = entry pointer (CF set if invalid)
__attribute__((carry_return)) __attribute__((preserve_register("cx"))) int fd_lookup(int file_descriptor __attribute__((in_register("bx"))), struct fd *result __attribute__((out_register("si"))));

// vfs_update_size: write fd position back to directory entry as file size (SI = fd entry)
void vfs_update_size(struct fd *entry __attribute__((in_register("si"))));

// vfs_find: look up path in VFS, populates vfs_found_*; CF on not found
__attribute__((carry_return)) int vfs_find(char *path __attribute__((in_register("si"))));

// vfs_create: create file at path in VFS, populates vfs_found_*; CF on error
__attribute__((carry_return)) int vfs_create(char *path __attribute__((in_register("si"))));

// fd_open_is_vga: return 1 if path == "/dev/vga", 0 otherwise
__attribute__((carry_return)) int fd_open_is_vga(char *path __attribute__((in_register("si"))));

// fd_populate_from_vfs: fill fd entry from vfs_found_* globals; SI=entry, AX=open_flags
void fd_populate_from_vfs(struct fd *entry __attribute__((in_register("si"))), int open_flags __attribute__((in_register("ax"))));

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
;;; fd_open_is_vga: Test if path equals \"/dev/vga\"
;;; Input:  SI = path
;;; Output: CF clear = match (return 1), CF set = no match (return 0)
;;; -----------------------------------------------------------------------
fd_open_is_vga:
        push cx
        push di
        push si
        mov di, DEV_VGA_PATH
        mov cx, 9               ; \"/dev/vga\" + null
        cld
        repe cmpsb
        pop si
        pop di
        pop cx
        jne .not_vga
        clc
        ret
        .not_vga:
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_populate_from_vfs: Fill fd entry from vfs_found_* globals
;;; Input:  SI = fd entry pointer, AX = open_flags (AL used)
;;; Clobbers: CX
;;; -----------------------------------------------------------------------
fd_populate_from_vfs:
        push cx
        mov cl, [vfs_found_type]
        mov [si+FD_OFFSET_TYPE], cl
        mov [si+FD_OFFSET_FLAGS], al
        mov cl, [vfs_found_mode]
        mov [si+FD_OFFSET_MODE], cl
        mov cx, [vfs_found_inode]
        mov [si+FD_OFFSET_START], cx
        mov cx, [vfs_found_size]
        mov [si+FD_OFFSET_SIZE], cx
        mov cx, [vfs_found_size+2]
        mov [si+FD_OFFSET_SIZE+2], cx
        mov word [si+FD_OFFSET_POSITION], 0
        mov word [si+FD_OFFSET_POSITION+2], 0
        mov cx, [vfs_found_dir_sec]
        mov [si+FD_OFFSET_DIRECTORY_SECTOR], cx
        mov cx, [vfs_found_dir_off]
        mov [si+FD_OFFSET_DIRECTORY_OFFSET], cx
        test al, O_TRUNC
        jz .populate_done
        mov word [si+FD_OFFSET_SIZE], 0
        mov word [si+FD_OFFSET_SIZE+2], 0
        .populate_done:
        pop cx
        ret

;;; -----------------------------------------------------------------------
;;; fd_read / fd_write: Table-driven dispatch via fd_ops.
;;;
;;; fd_ops is a flat table of (read_fn, write_fn) word pairs indexed by
;;; FD_TYPE_*.  A zero entry means the operation is unsupported for that
;;; type.  Adding a new fd type requires only a new row in fd_ops -- the
;;; dispatch functions need no changes.
;;; -----------------------------------------------------------------------
fd_read:
        call fd_lookup
        jc .err
        xor bh, bh
        mov bl, [si+FD_OFFSET_TYPE]
        shl bx, 2               ; * 4: each ops entry is two words
        mov ax, [fd_ops+bx]     ; read_fn
        test ax, ax
        jz .err
        jmp ax
        .err:
        mov ax, -1
        stc
        ret

fd_write:
        mov [fd_write_buffer], si
        call fd_lookup
        jc .err
        xor bh, bh
        mov bl, [si+FD_OFFSET_TYPE]
        shl bx, 2               ; * 4: each ops entry is two words
        mov ax, [fd_ops+bx+2]   ; write_fn
        test ax, ax
        jz .err
        jmp ax
        .err:
        mov ax, -1
        stc
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

;;; -----------------------------------------------------------------------
;;; fd_ioctl: Device-control dispatch.  Looks up BX=fd, then jumps to the
;;; per-type ioctl handler in fd_ioctl_ops.  Handler receives AL=cmd plus
;;; cmd-specific args in other registers and returns CF=0/1.
;;; -----------------------------------------------------------------------
fd_ioctl:
        call fd_lookup
        jc .err
        xor bh, bh
        mov bl, [si+FD_OFFSET_TYPE]
        shl bx, 1               ; one word per entry
        mov bx, [fd_ioctl_ops+bx]
        test bx, bx
        jz .err
        jmp bx
        .err:
        stc
        ret

        ;; Ioctl dispatch table indexed by FD_TYPE_*.  Zero = unsupported.
fd_ioctl_ops:
        dw 0                    ; FD_TYPE_FREE (0)
        dw 0                    ; FD_TYPE_CONSOLE (1)
        dw 0                    ; FD_TYPE_DIRECTORY (2)
        dw 0                    ; FD_TYPE_FILE (3)
        dw 0                    ; FD_TYPE_ICMP (4)
        dw 0                    ; FD_TYPE_NET (5)
        dw 0                    ; FD_TYPE_UDP (6)
        dw fd_ioctl_vga         ; FD_TYPE_VGA (7)

        ;; Variables
        DEV_VGA_PATH    db \"/dev/vga\", 0
        fd_write_buffer dw 0

%include \"fs/fd/console.asm\"
%include \"fs/fd/fs.asm\"
%include \"fs/fd/net.asm\"
");
