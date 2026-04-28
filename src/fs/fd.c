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

// ne2k_receive: Poll the NIC for one frame
//   Output: DI = NET_RECEIVE_BUFFER, CX = frame length, CF set if no packet.
__attribute__((carry_return)) int ne2k_receive(
    uint8_t *frame __attribute__((out_register("di"))),
    int *length __attribute__((out_register("cx"))));

// ne2k_send: Transmit an Ethernet frame (SI=buffer, CX=length; CF on error)
__attribute__((carry_return)) int ne2k_send(
    uint8_t *buffer __attribute__((in_register("si"))),
    int length __attribute__((in_register("cx"))));

// vfs_read_sec: Read sector containing fd's current position into SECTOR_BUFFER
//   Input: SI=fd_entry; Output: BX=byte offset within sector, CF on err
__attribute__((carry_return)) int vfs_read_sec(
    struct fd *entry __attribute__((in_register("si"))),
    int *byte_offset __attribute__((out_register("bx"))));

// vfs_prepare_write_sec: Prep SECTOR_BUFFER for write at fd's current position
//   Input: SI=fd_entry; Output: BX=byte offset within sector, CF on err
__attribute__((carry_return)) int vfs_prepare_write_sec(
    struct fd *entry __attribute__((in_register("si"))),
    int *byte_offset __attribute__((out_register("bx"))));

// vfs_commit_write_sec: Flush SECTOR_BUFFER back to disk (SI=fd_entry; CF on err)
__attribute__((carry_return)) int vfs_commit_write_sec(
    struct fd *entry __attribute__((in_register("si"))));

// fd_advance_position: Advance fd entry's 32-bit position by `delta` bytes.
//   Defined in fd.c's asm block; uses sub/sbb to propagate carry to position_hi,
//   which the pure-C idiom can't express through cc.py's signed-only compares.
void fd_advance_position(
    struct fd *entry __attribute__((in_register("si"))),
    int delta __attribute__((in_register("cx"))));

// ps2_check: CF clear if a decoded key is ready, CF set otherwise.
// Defined in drivers/ps2.c (was the ZF-returning ps2_check in
// drivers/ps2.asm with a CF-translating wrapper here; the C port
// returns CF directly so the wrapper is gone).
__attribute__((carry_return)) int ps2_check();

// ps2_read: Block until a decoded key is ready; returns AL=ASCII / AH=scan-code
// packed into AX (so the int return holds both halves; callers split via
// ``& 0xFF`` and ``>> 8``).
int ps2_read();

// put_character: Write one byte to screen + serial (AL = byte; preserves regs).
void put_character(int byte __attribute__((in_register("ax"))));

// serial_pushback_*: 2-slot FIFO drained at the top of fd_read_console.
// No fill site exists today; the drain path is preserved verbatim from the
// asm version.  When/if pushback gains a writer, it'll plug in here.
uint8_t serial_pushback_buffer[2];
uint8_t serial_pushback_count;

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

// fd_read_console: Read up to count bytes from keyboard / serial / pushback.
//   Returns after the first key event:
//     - normal ASCII key: 1 byte
//     - serial byte: 1 byte (passed through as-is)
//     - keyboard arrow key: 3 bytes (ESC '[' A/B/C/D); requires count >= 3
//   AX=bytes returned (0 if count==0); CF clear (no error path).
__attribute__((carry_return)) int fd_read_console(
    struct fd *entry __attribute__((in_register("si"))),
    uint8_t *user_buffer __attribute__((in_register("di"))),
    int count __attribute__((in_register("cx"))),
    int *bytes_read __attribute__((out_register("ax")))) {
    int status;
    int packed;
    int ascii;
    int scan_code;
    int third;
    if (count == 0) { *bytes_read = 0; return 1; }
    if (serial_pushback_count > 0) {
        user_buffer[0] = serial_pushback_buffer[0];
        serial_pushback_buffer[0] = serial_pushback_buffer[1];
        serial_pushback_count = serial_pushback_count - 1;
        *bytes_read = 1;
        return 1;
    }
    while (1) {
        // sti so PIT IRQ 0 advances system_ticks while the shell idles —
        // INT 30h enters with IF=0 and nothing else re-enables it before us.
        asm("sti");
        status = kernel_inb(0x3FD);
        if (status & 0x01) {
            user_buffer[0] = kernel_inb(0x3F8);
            *bytes_read = 1;
            return 1;
        }
        if (!ps2_check()) { continue; }
        packed = ps2_read();
        ascii = packed & 0xFF;
        if (ascii != 0) {
            user_buffer[0] = ascii;
            *bytes_read = 1;
            return 1;
        }
        // Extended key — need ESC '[' <letter>; skip if buffer < 3.
        if (count < 3) { continue; }
        scan_code = (packed >> 8) & 0xFF;
        if (scan_code == 0x48) { third = 'A'; }
        else if (scan_code == 0x50) { third = 'B'; }
        else if (scan_code == 0x4D) { third = 'C'; }
        else if (scan_code == 0x4B) { third = 'D'; }
        else { continue; }
        user_buffer[0] = 0x1B;
        user_buffer[1] = '[';
        user_buffer[2] = third;
        *bytes_read = 3;
        return 1;
    }
}

// fd_read_file: Read from a file fd
//   Input: SI=fd_entry, DI=user_buf, CX=count; Output: AX=bytes_read, CF on disk error.
//   Reads sector-by-sector via vfs_read_sec, copying min(512-byte_offset, left) bytes
//   from SECTOR_BUFFER to the user buffer each iteration.
__attribute__((carry_return)) int fd_read_file(
    struct fd *entry __attribute__((in_register("si"))),
    uint8_t *user_buffer __attribute__((in_register("di"))),
    int count __attribute__((in_register("cx"))),
    int *bytes_read __attribute__((out_register("ax")))) {
    uint16_t size_lo;
    uint16_t size_hi;
    uint16_t pos_lo;
    uint16_t pos_hi;
    int byte_offset;
    int chunk;
    int done;
    int left;
    int remaining;
    uint8_t *src;
    size_lo = entry->size_lo;
    size_hi = entry->size_hi;
    pos_lo = entry->position_lo;
    pos_hi = entry->position_hi;
    if (pos_hi > size_hi) { *bytes_read = 0; return 1; }
    if (pos_hi == size_hi && pos_lo >= size_lo) { *bytes_read = 0; return 1; }
    if (pos_hi == size_hi) {
        remaining = size_lo - pos_lo;
        if (count > remaining) { count = remaining; }
    }
    done = 0;
    left = count;
    while (left > 0) {
        if (!vfs_read_sec(entry, &byte_offset)) { *bytes_read = -1; return 0; }
        chunk = 512 - byte_offset;
        if (chunk > left) { chunk = left; }
        src = SECTOR_BUFFER;
        memcpy(user_buffer + done, src + byte_offset, chunk);
        done = done + chunk;
        left = left - chunk;
        fd_advance_position(entry, chunk);
    }
    *bytes_read = done;
    return 1;
}

// fd_read_net: Poll NIC for one frame; copy min(pkt_len, count) bytes to user_buf.
//   AX = bytes copied (0 = no packet ready), CF clear (never errors).
__attribute__((carry_return)) int fd_read_net(
    struct fd *entry __attribute__((in_register("si"))),
    uint8_t *user_buffer __attribute__((in_register("di"))),
    int user_count __attribute__((in_register("cx"))),
    int *bytes_read __attribute__((out_register("ax")))) {
    uint8_t *frame;
    int packet_length;
    if (!ne2k_receive(&frame, &packet_length)) {
        *bytes_read = 0;
        return 1;
    }
    if (packet_length > user_count) { packet_length = user_count; }
    memcpy(user_buffer, frame, packet_length);
    *bytes_read = packet_length;
    return 1;
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

// fd_write_console: Write count bytes from fd_write_buffer through put_character
//   (which mirrors them to screen + COM1 with ANSI parsing).  AX=count; CF clear.
__attribute__((carry_return)) int fd_write_console(
    struct fd *entry __attribute__((in_register("si"))),
    int count __attribute__((in_register("cx"))),
    int *bytes_written __attribute__((out_register("ax")))) {
    uint8_t *src;
    int written;
    src = fd_write_buffer;
    written = 0;
    while (written < count) {
        put_character(src[written]);
        written = written + 1;
    }
    *bytes_written = written;
    return 1;
}

// fd_write_file: Write to a file fd
//   Input: SI=fd_entry, CX=count (buffer in fd_write_buffer global)
//   Output: AX=bytes_written, CF on disk error.
//   Each iteration reads-modify-writes one sector via the prepare/commit helpers.
__attribute__((carry_return)) int fd_write_file(
    struct fd *entry __attribute__((in_register("si"))),
    int count __attribute__((in_register("cx"))),
    int *bytes_written __attribute__((out_register("ax")))) {
    int byte_offset;
    int chunk;
    int done;
    int left;
    uint8_t *dst;
    uint8_t *src;
    done = 0;
    left = count;
    while (left > 0) {
        if (!vfs_prepare_write_sec(entry, &byte_offset)) { *bytes_written = -1; return 0; }
        chunk = 512 - byte_offset;
        if (chunk > left) { chunk = left; }
        dst = SECTOR_BUFFER;
        src = fd_write_buffer;
        memcpy(dst + byte_offset, src + done, chunk);
        if (!vfs_commit_write_sec(entry)) { *bytes_written = -1; return 0; }
        done = done + chunk;
        left = left - chunk;
        fd_advance_position(entry, chunk);
    }
    *bytes_written = done;
    return 1;
}

// fd_write_net: Send a raw Ethernet frame from the user buffer.
//   AX = count on success / -1 on error; CF set on error.
__attribute__((carry_return)) int fd_write_net(
    struct fd *entry __attribute__((in_register("si"))),
    int count __attribute__((in_register("cx"))),
    int *bytes_written __attribute__((out_register("ax")))) {
    if (!ne2k_send(fd_write_buffer, count)) {
        *bytes_written = -1;
        return 0;
    }
    *bytes_written = count;
    return 1;
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
;;; fd_advance_position: 32-bit add of CX into entry's position field
;;;     (sub/sbb-style carry propagation; pure-C cannot express this through
;;;     cc.py's signed-only `<` codegen).
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

fd_advance_position:
        add [si+FD_OFFSET_POSITION], cx
        adc word [si+FD_OFFSET_POSITION+2], 0
        ret

        ;; Operations table: (read_fn, write_fn) indexed by FD_TYPE_*
        ;; A zero entry means unsupported for that type.  vfs_read_dir is
        ;; routed directly (its own jmp-trampoline matches the read_fn ABI).
fd_ops:
        dw 0,               0                 ; FD_TYPE_FREE (0)
        dw fd_read_console, fd_write_console  ; FD_TYPE_CONSOLE (1)
        dw vfs_read_dir,    0                 ; FD_TYPE_DIRECTORY (2)
        dw fd_read_file,    fd_write_file     ; FD_TYPE_FILE (3)
        dw 0,               0                 ; FD_TYPE_ICMP (4)
        dw fd_read_net,     fd_write_net      ; FD_TYPE_NET (5)
        dw 0,               0                 ; FD_TYPE_UDP (6)
        dw 0,               0                 ; FD_TYPE_VGA (7)

        fd_write_buffer dw 0
");
