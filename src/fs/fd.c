// fs/fd.c — File descriptor table management.
//
// Fully ported to C: the five simple helpers (fd_alloc, fd_close,
// fd_fstat, fd_init, fd_lookup) plus the four dispatchers (fd_open,
// fd_read, fd_write, fd_ioctl) and their dispatch tables.  The read /
// write dispatchers tail-call through cc.py's __tail_call into the
// per-fd-type handlers in fs/fd/{console,fs,net}.c; fd_ioctl pins the
// function pointer to EBX so the cmd byte in AL survives the jump.
//
// The trailing asm() block is just the ``%include`` directives that
// pull the per-fd-type handler bodies into the same NASM scope.
//
// Calling conventions (input/output registers, CF semantics) match
// the original asm so external callers (syscall.asm and the
// per-fd-type handlers) link unchanged.

// Layout used by the helpers and the asm dispatchers; matches the
// FD_OFFSET_* / FD_ENTRY_SIZE constants in include/constants.asm.
//
// event_head / event_tail / event_buf form a per-fd PS/2 event ring
// (FD_TYPE_CONSOLE only).  The C code never touches the ring directly
// — the producer is an asm broadcaster in drivers/ps2.c that walks
// the table from IRQ context, and the consumer is the inline asm in
// fs/fd/console.c that drains a single fd's slots when userspace
// calls CONSOLE_IOCTL_TRY_GET_EVENT.  event_buf is declared as bytes
// because cc.py doesn't carry int-array struct fields end-to-end;
// the asm sides reach in via FD_OFFSET_EVENT_BUF + index*4 for the
// 32-bit (pressed << 16) | bbkey slots.
struct fd {
    uint8_t type;
    uint8_t flags;
    uint16_t start;
    int size;
    int position;
    uint16_t directory_sector;
    uint16_t directory_offset;
    uint8_t mode;
    uint8_t event_head;
    uint8_t event_tail;
    uint8_t _pad;
    uint8_t event_buf[32];
    uint8_t _rest[12];
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

// fs/vfs.asm: locates a file (vfs_find) or creates one (vfs_create);
// both populate the vfs_found_* cluster and return CF clear on success.
__attribute__((carry_return))
int vfs_find(uint8_t *path __attribute__((in_register("esi"))));
__attribute__((carry_return))
int vfs_create(uint8_t *path __attribute__((in_register("esi"))));

// vfs.asm globals populated by vfs_find / vfs_create.  Read by
// fd_open after the call to populate the new fd entry.  ``size`` is
// 32-bit; the asm side stores it as ``dd``.  ``inode`` doubles as
// ``start sector`` for bbfs and ``inode number`` for ext2.  Use
// ``asm_name`` rather than ``extern`` because the asm side owns the
// labels and uses the bare names (``vfs_found_size``, not
// ``_g_vfs_found_size``); ``extern`` would emit ``_g_<name>``
// references that NASM can't resolve.
uint8_t vfs_found_type __attribute__((asm_name("vfs_found_type")));
uint8_t vfs_found_mode __attribute__((asm_name("vfs_found_mode")));
uint16_t vfs_found_inode __attribute__((asm_name("vfs_found_inode")));
uint32_t vfs_found_size __attribute__((asm_name("vfs_found_size")));
uint16_t vfs_found_dir_sec __attribute__((asm_name("vfs_found_dir_sec")));
uint16_t vfs_found_dir_off __attribute__((asm_name("vfs_found_dir_off")));

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
// live in fs/fd/{audio,console,fs,net}.c; only the symbol identity
// matters for the static initializer below.  fd_close_audio is called
// directly from fd_close (not via the fd_ops table) so the per-type
// teardown stays a one-liner alongside the existing FD_TYPE_FILE
// flush branch.
void fd_close_audio();
void fd_close_midi();
int fd_ioctl_audio();
int fd_ioctl_console();
int fd_ioctl_midi();
int fd_ioctl_vga();
int fd_read_console();
int fd_read_dir();
int fd_read_file();
int fd_read_net();
int fd_write_audio();
int fd_write_console();
int fd_write_file();
int fd_write_midi();
int fd_write_net();
void midi_reset_state();

// Set by drivers/sb16.c on successful DSP probe.  Read by fd_open's
// /dev/audio branch (refuses open when 0).  Mirrors the asm-name
// shim used in drivers/ne2k.c so cc.py emits the bare name reference
// the asm-side `_g_sb16_present` can satisfy at link time.
uint8_t sb16_present __attribute__((asm_name("_g_sb16_present")));

// Set by drivers/opl3.c on successful OPL3 probe.  Read by fd_open's
// /dev/midi branch (refuses open when 0).  Same asm-name shim as
// sb16_present so cc.py emits the bare name reference the asm-side
// `_g_opl3_present` can satisfy at link time.
uint8_t opl3_present __attribute__((asm_name("_g_opl3_present")));

// Forward decls for the SB16 driver's per-open / per-close callbacks.
// sb16_open allocates the DMA frame and arms the controller; called
// from fd_open's /dev/audio branch.  sb16_close tears down; called
// from fd_close when the entry type is FD_TYPE_AUDIO.  Both are
// stubbed in early tasks and filled in tasks 7 and 10.
__attribute__((carry_return))
int sb16_open();
void sb16_close();

struct fd_ops_entry fd_ops[10] = {
    { 0,               0 },                 // FD_TYPE_FREE (0)
    { 0,               fd_write_audio },    // FD_TYPE_AUDIO (1)
    { fd_read_console, fd_write_console },  // FD_TYPE_CONSOLE (2)
    { fd_read_dir,     0 },                 // FD_TYPE_DIRECTORY (3)
    { fd_read_file,    fd_write_file },     // FD_TYPE_FILE (4)
    { 0,               0 },                 // FD_TYPE_ICMP (5)
    { 0,               fd_write_midi },     // FD_TYPE_MIDI (6)
    { fd_read_net,     fd_write_net },      // FD_TYPE_NET (7)
    { 0,               0 },                 // FD_TYPE_UDP (8)
    { 0,               0 },                 // FD_TYPE_VGA (9)
};

// fd_ioctl dispatch table — one ioctl entry per FD_TYPE_*.  A 0 slot
// means "no ioctl support".  Wrapped in a one-field struct because
// cc.py rejects ``int (*name[N])()`` array-of-function_pointer at
// file scope; the struct workaround is identical at the byte level.
struct fd_ioctl_op {
    int (*ioctl)();
};

struct fd_ioctl_op fd_ioctl_ops[10] = {
    { 0 },                  // FD_TYPE_FREE (0)
    { fd_ioctl_audio },     // FD_TYPE_AUDIO (1)
    { fd_ioctl_console },   // FD_TYPE_CONSOLE (2)
    { 0 },                  // FD_TYPE_DIRECTORY (3)
    { 0 },                  // FD_TYPE_FILE (4)
    { 0 },                  // FD_TYPE_ICMP (5)
    { fd_ioctl_midi },      // FD_TYPE_MIDI (6)
    { 0 },                  // FD_TYPE_NET (7)
    { 0 },                  // FD_TYPE_UDP (8)
    { fd_ioctl_vga },       // FD_TYPE_VGA (9)
};

// fd_table — kernel BSS, FD_MAX entries × FD_ENTRY_SIZE bytes.  The asm
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
    if (entry->type == FD_TYPE_AUDIO) {
        sb16_close();
    } else if (entry->type == FD_TYPE_FILE) {
        if ((entry->flags & O_WRONLY) != 0) {
            vfs_update_size(entry);
        }
    } else if (entry->type == FD_TYPE_MIDI) {
        fd_close_midi();
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
// — fd_ioctl_vga reads AL directly to pick the sub-command.  ECX/EDX
// are preserved across the dispatch (this body does ``mov ecx, eax``
// for the array-index multiply and the nested fd_lookup writes EDX)
// so VGA_IOCTL_FILL_BLOCK / VGA_IOCTL_MODE / VGA_IOCTL_SET_PALETTE
// see the user's CX / DL / DX intact in fd_ioctl_vga.  Error path:
// ``stc; ret`` with AX left at whatever the syscall layer preserved
// (matching the asm version's contract).
__attribute__((carry_return))
__attribute__((preserve_register("ecx")))
__attribute__((preserve_register("edx")))
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
// if the fd is out of range or its slot is FD_TYPE_FREE.  ECX/EDX
// are preserved (this body lands ``mov edx, eax`` on the entry-pointer
// computation) so callers further up the dispatch chain — fd_ioctl,
// fd_read, fd_write — can keep CX/DL/DX live across the lookup; that
// matters for VGA ioctls (DL=mode/color, CL/CH=row/col) and
// console writes that latch CX through to the per-type handler.
__attribute__((carry_return))
__attribute__((preserve_register("ecx")))
__attribute__((preserve_register("edx")))
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

// fd_open: open the file at `name` with the given `flags`, returning
// AX = fd or -1 (CF set on error).  /dev/vga is a synthetic device
// that bypasses the filesystem and just allocates an FD_TYPE_VGA
// slot.  Otherwise vfs_find populates vfs_found_*; if not found and
// O_CREAT is set, vfs_create makes a fresh entry.  The new fd's
// fields come from the vfs_found_* cluster, except O_TRUNC zeros
// the size so a subsequent write rebuilds the file from scratch.
__attribute__((carry_return))
int fd_open(int *result __attribute__((out_register("ax"))),
            uint8_t *name __attribute__((in_register("esi"))),
            int flags __attribute__((in_register("ax")))) {
    int fd_num;
    struct fd *entry;
    if (memcmp(name, "/dev/vga", 9) == 0) {
        if (!fd_alloc(&fd_num, &entry)) {
            *result = -1;
            return 0;
        }
        entry->type = FD_TYPE_VGA;
        entry->flags = flags;
        *result = fd_num;
        return 1;
    }
    // /dev/audio — refuse if SB16 absent or already opened by another fd
    // (single-opener; matches OSS /dev/dsp semantics).  sb16_open
    // allocates the DMA frame and starts the SB16 in auto-init
    // playback; close path tears it down via sb16_close in fd_close.
    if (memcmp(name, "/dev/audio", 11) == 0) {
        struct fd *cursor;
        int i;
        if (sb16_present == 0) {
            *result = -1;
            return 0;
        }
        cursor = fd_table;
        i = 0;
        while (i < FD_MAX) {
            if (cursor->type == FD_TYPE_AUDIO) {
                *result = -1;
                return 0;
            }
            cursor = cursor + 1;
            i = i + 1;
        }
        if (!sb16_open()) {
            *result = -1;
            return 0;
        }
        if (!fd_alloc(&fd_num, &entry)) {
            // sb16_open succeeded but no fd slot available.  Tear down
            // the device so we don't leak the DMA frame.  Rare in
            // practice (FD_MAX = 8).
            sb16_close();
            *result = -1;
            return 0;
        }
        entry->type = FD_TYPE_AUDIO;
        entry->flags = flags;
        *result = fd_num;
        return 1;
    }
    // /dev/midi — refuse if OPL3 absent or already opened by another fd
    // (single-opener; mirrors /dev/audio semantics).  midi_reset_state
    // zeros the queue + silences the chip so a fresh open starts clean.
    if (memcmp(name, "/dev/midi", 10) == 0) {
        struct fd *cursor;
        int i;
        if (opl3_present == 0) {
            *result = -1;
            return 0;
        }
        cursor = fd_table;
        i = 0;
        while (i < FD_MAX) {
            if (cursor->type == FD_TYPE_MIDI) {
                *result = -1;
                return 0;
            }
            cursor = cursor + 1;
            i = i + 1;
        }
        if (!fd_alloc(&fd_num, &entry)) {
            *result = -1;
            return 0;
        }
        entry->type = FD_TYPE_MIDI;
        entry->flags = flags;
        midi_reset_state();
        *result = fd_num;
        return 1;
    }
    if (!vfs_find(name)) {
        if ((flags & O_CREAT) == 0) {
            *result = -1;
            return 0;
        }
        if (!vfs_create(name)) {
            *result = -1;
            return 0;
        }
    }
    if (!fd_alloc(&fd_num, &entry)) {
        *result = -1;
        return 0;
    }
    entry->type = vfs_found_type;
    entry->flags = flags;
    entry->mode = vfs_found_mode;
    entry->start = vfs_found_inode;
    entry->size = vfs_found_size;
    entry->position = 0;
    entry->directory_sector = vfs_found_dir_sec;
    entry->directory_offset = vfs_found_dir_off;
    if ((flags & O_TRUNC) != 0) {
        entry->size = 0;
    }
    *result = fd_num;
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

// fd_seek: reposition the read/write cursor for a regular file fd.
// Inputs: BX = fd, ECX = signed offset, AL = whence (SEEK_SET=0,
// SEEK_CUR=1, SEEK_END=2).  Returns EAX = new absolute position
// (clamped to [0, size]), CF set on bad fd / wrong type / unknown
// whence.  Only FD_TYPE_FILE is seekable — sockets, console, and
// directories all error.  We clamp rather than fail on out-of-range
// because Doom's WAD reader sometimes seeks past EOF and expects the
// next read to return 0 bytes (EOF semantics).
__attribute__((carry_return))
int fd_seek(int *result __attribute__((out_register("ax"))),
            int fd_num __attribute__((in_register("bx"))),
            int offset __attribute__((in_register("ecx"))),
            int whence __attribute__((in_register("ax")))) {
    struct fd *entry;
    int new_position;
    if (!fd_lookup(fd_num, &entry)) {
        *result = -1;
        return 0;
    }
    if (entry->type != FD_TYPE_FILE) {
        *result = -1;
        return 0;
    }
    if (whence == SEEK_SET) {
        new_position = offset;
    } else if (whence == SEEK_CUR) {
        new_position = entry->position + offset;
    } else if (whence == SEEK_END) {
        new_position = entry->size + offset;
    } else {
        *result = -1;
        return 0;
    }
    if (new_position < 0) {
        new_position = 0;
    }
    if (new_position > entry->size) {
        new_position = entry->size;
    }
    entry->position = new_position;
    *result = new_position;
    return 1;
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

// All dispatchers are now C.  The remaining asm() block just brings
// in the per-fd-type handler %includes (fs/fd/console.kasm /
// fs.kasm / net.kasm) so their labels are visible at NASM-link
// time.
asm("%include \"fs/fd/audio.kasm\"\n"
    "%include \"fs/fd/console.kasm\"\n"
    "%include \"fs/fd/fs.kasm\"\n"
    "%include \"fs/fd/midi.kasm\"\n"
    "%include \"fs/fd/net.kasm\"\n");
