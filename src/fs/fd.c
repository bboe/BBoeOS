// fs/fd.c — File descriptor table management.
//
// Most of the file is now real C: the five simple helpers (fd_alloc,
// fd_close, fd_fstat, fd_init, fd_lookup) plus the read/write
// dispatchers (fd_read, fd_write) and the fd_ops table they index.
// The dispatchers tail-call through cc.py's __tail_call into the
// per-fd-type handlers in fs/fd/{console,fs,net}.c.
//
// Only fd_open still lives in inline asm.  It blocks on porting
// fs/vfs.asm — fd_open reads a cluster of vfs_found_* globals
// populated by vfs_find / vfs_create, which only become C-visible
// once vfs.asm itself ports (via cc.py's file-scope function_pointer
// support and an extern struct vfs_found global).
//
// Calling conventions (input/output registers, CF semantics) are
// preserved across the port so external callers (syscall.asm and the
// per-fd-type handlers) link unchanged.

// Layout used by the helpers and the asm dispatchers; matches the
// FD_OFFSET_* / FD_ENTRY_SIZE constants in include/constants.asm.
struct fd {
    uint8_t type;
    uint8_t flags;
    uint16_t start;
    int size;
    int position;
    uint16_t directory_sector;
    uint16_t directory_offset;
    uint8_t mode;
    uint8_t _rest[15];
};

// fd_lookup is forward-declared because fd_close calls it but the
// helpers are emitted in alphabetical order below (fd_close lands
// before fd_lookup).
__attribute__((carry_return)) __attribute__((preserve_register("ecx")))
int fd_lookup(int fd_num __attribute__((in_register("bx"))),
              struct fd *entry __attribute__((out_register("esi"))));

// fs/vfs.asm: writes the fd's final position back into the directory
// entry as the file size.  Used by fd_close on writable file fds.
__attribute__((carry_return))
int vfs_update_size(struct fd *entry __attribute__((in_register("esi"))));

// fd_ops dispatch table — one entry per FD_TYPE_*.  Each entry is a
// (read_fn, write_fn) pair; a 0 slot means "unsupported".  Indexed
// by fd entry's type byte.  The function-pointer fields are 4 bytes
// each in 32-bit mode, so each entry is 8 bytes; fd_read / fd_write
// compute (fd_ops + entry->type)->read / ->write to fetch the
// handler.  The struct field's parameter list is intentionally empty
// since cc.py doesn't carry function-pointer signatures through
// struct types — the dispatchers below redeclare the local
// function_pointer with the in_register annotations the handlers
// expect.
struct fd_ops_entry {
    int (*read)();
    int (*write)();
};

// Forward declarations for the per-fd-type handlers.  The bodies
// live in fs/fd/{console,fs,net}.c; only the symbol identity matters
// for the static initializer below.
int fd_ioctl_vga();
int fd_read_console();
int fd_read_dir();
int fd_read_file();
int fd_read_net();
int fd_write_console();
int fd_write_file();
int fd_write_net();

struct fd_ops_entry fd_ops[8] = {
    { 0,               0 },                 // FD_TYPE_FREE (0)
    { fd_read_console, fd_write_console },  // FD_TYPE_CONSOLE (1)
    { fd_read_dir,     0 },                 // FD_TYPE_DIRECTORY (2)
    { fd_read_file,    fd_write_file },     // FD_TYPE_FILE (3)
    { 0,               0 },                 // FD_TYPE_ICMP (4)
    { fd_read_net,     fd_write_net },      // FD_TYPE_NET (5)
    { 0,               0 },                 // FD_TYPE_UDP (6)
    { 0,               0 },                 // FD_TYPE_VGA (7)
};

// fd_ioctl dispatch table — one ioctl entry per FD_TYPE_*.  A 0 slot
// means "no ioctl support".  Wrapped in a one-field struct because
// cc.py rejects ``int (*name[N])()`` array-of-function_pointer at
// file scope; the struct workaround is identical at the byte level.
struct fd_ioctl_op {
    int (*ioctl)();
};

struct fd_ioctl_op fd_ioctl_ops[8] = {
    { 0 },                  // FD_TYPE_FREE (0)
    { 0 },                  // FD_TYPE_CONSOLE (1)
    { 0 },                  // FD_TYPE_DIRECTORY (2)
    { 0 },                  // FD_TYPE_FILE (3)
    { 0 },                  // FD_TYPE_ICMP (4)
    { 0 },                  // FD_TYPE_NET (5)
    { 0 },                  // FD_TYPE_UDP (6)
    { fd_ioctl_vga },       // FD_TYPE_VGA (7)
};

// fd_table — kernel BSS, FD_MAX entries × 32 bytes.  The asm
// dispatchers below (and the per-fd-type handlers in fs/fd/*.kasm)
// reach into entries via ``[esi+FD_OFFSET_*]``; they reference the
// bare ``fd_table`` symbol via the equ shim so they don't need to
// know cc.py's _g_ prefix.
struct fd fd_table[FD_MAX];
asm("fd_table equ _g_fd_table");

// fd_write_buffer — the dispatcher (fd_write below) stashes the
// caller-supplied user buffer pointer here before tail-jumping to the
// per-type write handler.  Hoisted out of asm so the C-ported
// handlers in fs/fd/{console,fs,net}.c can read it directly.
uint8_t *fd_write_buffer;
asm("fd_write_buffer equ _g_fd_write_buffer");

// fd_alloc: linear scan for the first FD_TYPE_FREE slot.  AX = fd
// number, ESI = entry pointer; CF set if the table is full.
__attribute__((carry_return))
int fd_alloc(int *fd_num __attribute__((out_register("ax"))),
             struct fd *entry __attribute__((out_register("esi")))) {
    int i;
    struct fd *cursor;
    cursor = fd_table;
    i = 0;
    while (i < FD_MAX) {
        if (cursor->type == FD_TYPE_FREE) {
            // Order matters: *entry's mov-to-ESI emission also leaves
            // EAX = cursor; *fd_num must follow so the trailing
            // expression eval lands the fd number in EAX/AX.
            *entry = cursor;
            *fd_num = i;
            return 1;
        }
        cursor = cursor + 1;
        i = i + 1;
    }
    return 0;
}

// fd_close: writable file fds flush their final position back to the
// directory entry via vfs_update_size; then every fd type zeros its
// slot (FD_TYPE_FREE = 0 by virtue of position 0 being the type
// field).  CF set if the fd was already free / out of range.
__attribute__((carry_return))
int fd_close(int fd_num __attribute__((in_register("bx")))) {
    struct fd *entry;
    if (!fd_lookup(fd_num, &entry)) {
        return 0;
    }
    if (entry->type == FD_TYPE_FILE) {
        if ((entry->flags & O_WRONLY) != 0) {
            vfs_update_size(entry);
        }
    }
    memset(entry, 0, FD_ENTRY_SIZE);
    return 1;
}

// fd_fstat: AL = mode (file permission flags), CX:DX = 32-bit size
// split (CX = high 16 bits, DX = low 16 bits).  CF set if the fd is
// invalid.  ``mode`` uses ``out_register("ax")`` rather than
// ``out_register("al")`` because the syscall dispatcher only looks at
// AL — emitting through AX (with the high byte cleared by the
// uint8_t-to-int widening) keeps the cc.py codegen path uniform with
// the CX/DX captures and avoids the byte-alias mismatch in the
// DerefAssign emission.
__attribute__((carry_return))
int fd_fstat(int *mode __attribute__((out_register("ax"))),
             int *size_high __attribute__((out_register("cx"))),
             int *size_low __attribute__((out_register("dx"))),
             int fd_num __attribute__((in_register("bx")))) {
    struct fd *entry;
    if (!fd_lookup(fd_num, &entry)) {
        return 0;
    }
    // Order matters: *size_low / *size_high emit explicit ``mov dx,
    // ax`` / ``mov cx, ax`` so each capture is durable.  *mode (the
    // ``out_register("ax")`` capture) skips the redundant ``mov ax,
    // ax`` and instead relies on the trailing expression eval leaving
    // EAX = mode at function exit, so it has to come last.
    *size_low = entry->size & 0xFFFF;
    *size_high = (entry->size >> 16) & 0xFFFF;
    *mode = entry->mode;
    return 1;
}

// fd_init: zero the fd table, then pre-open fds 0/1/2 as console.
void fd_init() {
    struct fd *cursor;
    memset(fd_table, 0, FD_MAX * FD_ENTRY_SIZE);
    cursor = fd_table;
    cursor->type = FD_TYPE_CONSOLE;
    cursor->flags = O_RDONLY;
    cursor = cursor + 1;
    cursor->type = FD_TYPE_CONSOLE;
    cursor->flags = O_WRONLY;
    cursor = cursor + 1;
    cursor->type = FD_TYPE_CONSOLE;
    cursor->flags = O_WRONLY;
}

// fd_ioctl: dispatch on entry->type into fd_ioctl_ops[type].ioctl.
// Inputs are AL = cmd, BX = fd, plus per-(type, cmd) extras (ECX/EDX)
// that flow through to the handler unchanged.  The function pointer
// is pinned to EBX so the tail-jump (``jmp ebx``) doesn't clobber AL
// — fd_ioctl_vga reads AL directly to pick the sub-command.  Error
// path: ``stc; ret`` with AX left at whatever the syscall layer
// preserved (matching the asm version's contract).
__attribute__((carry_return))
int fd_ioctl(int cmd __attribute__((in_register("ax"))),
             int fd_num __attribute__((in_register("bx")))) {
    struct fd *entry;
    struct fd_ioctl_op *op;
    int (*handler)(int c __attribute__((in_register("ax"))),
                   struct fd *e __attribute__((in_register("esi"))))
                   __attribute__((pinned_register("ebx")));
    if (!fd_lookup(fd_num, &entry)) {
        return 0;
    }
    op = fd_ioctl_ops + entry->type;
    handler = op->ioctl;
    if (handler == 0) {
        return 0;
    }
    __tail_call(handler, cmd, entry);
}

// fd_lookup: validate fd in BX, return ESI = entry pointer.  CF set
// if the fd is out of range or its slot is FD_TYPE_FREE.  ECX is
// preserved so the asm fd_open / fd_read / fd_write dispatchers can
// keep ECX live across the call.
__attribute__((carry_return)) __attribute__((preserve_register("ecx")))
int fd_lookup(int fd_num __attribute__((in_register("bx"))),
              struct fd *entry __attribute__((out_register("esi")))) {
    struct fd *cursor;
    if (fd_num >= FD_MAX) {
        return 0;
    }
    cursor = fd_table + fd_num;
    if (cursor->type == FD_TYPE_FREE) {
        return 0;
    }
    *entry = cursor;
    return 1;
}

// fd_read: dispatch on entry->type into fd_ops[type].read.  Inputs
// are BX = fd, EDI = user buffer, ECX = byte count.  fd_lookup
// preserves ECX and EDI; the C frame spills them to slots and reloads
// them just before the tail-jump.  Error path matches the asm-side
// contract: AX = -1, CF set.  The handler's own AX/CF flow back
// through the tail-jump unchanged.
__attribute__((carry_return))
int fd_read(int *result __attribute__((out_register("ax"))),
            int fd_num __attribute__((in_register("bx"))),
            uint8_t *buffer __attribute__((in_register("edi"))),
            int count __attribute__((in_register("ecx")))) {
    struct fd *entry;
    struct fd_ops_entry *ops;
    int (*handler)(struct fd *e __attribute__((in_register("esi"))),
                   uint8_t *b __attribute__((in_register("edi"))),
                   int c __attribute__((in_register("ecx"))));
    if (!fd_lookup(fd_num, &entry)) {
        *result = -1;
        return 0;
    }
    ops = fd_ops + entry->type;
    handler = ops->read;
    if (handler == 0) {
        *result = -1;
        return 0;
    }
    __tail_call(handler, entry, buffer, count);
}

// fd_write: dispatch on entry->type into fd_ops[type].write.  Inputs
// are BX = fd, ESI = source buffer, ECX = byte count.  Stash ESI into
// fd_write_buffer first (fd_lookup overwrites ESI with the entry
// pointer); the per-type handlers read fd_write_buffer to fetch the
// source bytes.  Error path: AX = -1, CF set, same as fd_read.
__attribute__((carry_return))
int fd_write(int *result __attribute__((out_register("ax"))),
             int fd_num __attribute__((in_register("bx"))),
             uint8_t *source __attribute__((in_register("esi"))),
             int count __attribute__((in_register("ecx")))) {
    struct fd *entry;
    struct fd_ops_entry *ops;
    int (*handler)(struct fd *e __attribute__((in_register("esi"))),
                   int c __attribute__((in_register("ecx"))));
    fd_write_buffer = source;
    if (!fd_lookup(fd_num, &entry)) {
        *result = -1;
        return 0;
    }
    ops = fd_ops + entry->type;
    handler = ops->write;
    if (handler == 0) {
        *result = -1;
        return 0;
    }
    __tail_call(handler, entry, count);
}

// fd_open / fd_ioctl + their data and per-fd-type %include directives
// stay in inline asm.  fd_open blocks on multi-output vfs_find /
// vfs_create (it reads a cluster of vfs_found_* globals after each
// call).  fd_ioctl blocks on the AL/EAX register conflict: the
// syscall dispatcher delivers cmd in AL, but cc.py's __tail_call
// hardcodes the function pointer in EAX — the asm version
// specifically uses ``mov ebx, [...]; jmp ebx`` to keep AL intact for
// the handler.
asm("fd_open:\n"
"        push ecx\n"
"        push edx\n"
"        push edi\n"
"        mov [fd_open_flags], al\n"
"        mov [fd_open_name], esi\n"
"        ;; Check synthetic device paths first (no filesystem lookup).\n"
"        mov edi, DEV_VGA_PATH\n"
"        mov ecx, 9                      ; \"/dev/vga\" + null\n"
"        cld\n"
"        repe cmpsb\n"
"        jne .open_not_device\n"
"        call fd_alloc\n"
"        jc .open_err\n"
"        mov byte [esi+FD_OFFSET_TYPE], FD_TYPE_VGA\n"
"        mov cl, [fd_open_flags]\n"
"        mov [esi+FD_OFFSET_FLAGS], cl\n"
"        mov [fd_open_fd], ax\n"
"        jmp .open_done\n"
"        .open_not_device:\n"
"        mov esi, [fd_open_name]\n"
"        ;; Look up the file (vfs_find handles \".\" -> root directory)\n"
"        call vfs_find           ; populates vfs_found_*\n"
"        jc .open_not_found\n"
"        jmp .open_populate\n"
"\n"
"        .open_not_found:\n"
"        ;; If O_CREAT is set, create the file\n"
"        test byte [fd_open_flags], O_CREAT\n"
"        jz .open_err\n"
"        mov esi, [fd_open_name]\n"
"        call vfs_create         ; SI=path -> vfs_found_*, CF on error\n"
"        jc .open_err\n"
"        jmp .open_populate\n"
"\n"
"        .open_populate:\n"
"        ;; vfs_found_* is now fully populated\n"
"        call fd_alloc\n"
"        jc .open_err\n"
"        mov [fd_open_fd], ax\n"
"        ;; Type, flags, mode, inode, size, position from vfs_found_*\n"
"        mov cl, [vfs_found_type]\n"
"        mov [esi+FD_OFFSET_TYPE], cl\n"
"        mov cl, [fd_open_flags]\n"
"        mov [esi+FD_OFFSET_FLAGS], cl\n"
"        mov cl, [vfs_found_mode]\n"
"        mov [esi+FD_OFFSET_MODE], cl\n"
"        mov cx, [vfs_found_inode]\n"
"        mov [esi+FD_OFFSET_START], cx\n"
"        mov cx, [vfs_found_size]\n"
"        mov [esi+FD_OFFSET_SIZE], cx\n"
"        mov cx, [vfs_found_size+2]\n"
"        mov [esi+FD_OFFSET_SIZE+2], cx\n"
"        mov dword [esi+FD_OFFSET_POSITION], 0\n"
"        mov cx, [vfs_found_dir_sec]\n"
"        mov [esi+FD_OFFSET_DIRECTORY_SECTOR], cx\n"
"        mov cx, [vfs_found_dir_off]\n"
"        mov [esi+FD_OFFSET_DIRECTORY_OFFSET], cx\n"
"        ;; O_TRUNC: reset size to 0\n"
"        test byte [fd_open_flags], O_TRUNC\n"
"        jz .open_done\n"
"        mov word [esi+FD_OFFSET_SIZE], 0\n"
"        mov word [esi+FD_OFFSET_SIZE+2], 0\n"
"        .open_done:\n"
"        mov ax, [fd_open_fd]\n"
"        pop edi\n"
"        pop edx\n"
"        pop ecx\n"
"        clc\n"
"        ret\n"
"\n"
"        .open_err:\n"
"        pop edi\n"
"        pop edx\n"
"        pop ecx\n"
"        mov ax, -1\n"
"        stc\n"
"        ret\n"
"\n"
"%include \"fs/fd/console.kasm\"\n"
"%include \"fs/fd/fs.kasm\"\n"
"%include \"fs/fd/net.kasm\"\n"
"\n"
"        DEV_VGA_PATH    db \"/dev/vga\", 0\n"
"        fd_open_fd      dw 0\n"
"        fd_open_flags   db 0\n"
"        fd_open_mode    db 0\n"
"        fd_open_name    dd 0\n"
);
