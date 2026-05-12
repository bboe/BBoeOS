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

#include "pipe.h"
#include "program_state.h"

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
    uint8_t dirty;
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
// Sized [12] after PIPE_R(8) and PIPE_W(9) were inserted, renumbering
// UDP(8→10) and VGA(9→11).
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
int fd_read_pipe();
int fd_write_audio();
int fd_write_console();
int fd_write_file();
int fd_write_midi();
int fd_write_net();
int fd_write_pipe();
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

struct fd_ops_entry fd_ops[12] = {
    { 0,               0 },                 // FD_TYPE_FREE (0)
    { 0,               fd_write_audio },    // FD_TYPE_AUDIO (1)
    { fd_read_console, fd_write_console },  // FD_TYPE_CONSOLE (2)
    { fd_read_dir,     0 },                 // FD_TYPE_DIRECTORY (3)
    { fd_read_file,    fd_write_file },     // FD_TYPE_FILE (4)
    { 0,               0 },                 // FD_TYPE_ICMP (5)
    { 0,               fd_write_midi },     // FD_TYPE_MIDI (6)
    { fd_read_net,     fd_write_net },      // FD_TYPE_NET (7)
    { fd_read_pipe,    0 },                 // FD_TYPE_PIPE_R (8)
    { 0,               fd_write_pipe },     // FD_TYPE_PIPE_W (9)
    { 0,               0 },                 // FD_TYPE_UDP (10)
    { 0,               0 },                 // FD_TYPE_VGA (11)
};

// fd_ioctl dispatch table — one ioctl entry per FD_TYPE_*.  A 0 slot
// means "no ioctl support".  Wrapped in a one-field struct because
// cc.py rejects ``int (*name[N])()`` array-of-function_pointer at
// file scope; the struct workaround is identical at the byte level.
// Sized [12] after PIPE_R(8) and PIPE_W(9) were inserted, renumbering
// UDP(8→10) and VGA(9→11).
struct fd_ioctl_op {
    int (*ioctl)();
};

struct fd_ioctl_op fd_ioctl_ops[12] = {
    { 0 },                  // FD_TYPE_FREE (0)
    { fd_ioctl_audio },     // FD_TYPE_AUDIO (1)
    { fd_ioctl_console },   // FD_TYPE_CONSOLE (2)
    { 0 },                  // FD_TYPE_DIRECTORY (3)
    { 0 },                  // FD_TYPE_FILE (4)
    { 0 },                  // FD_TYPE_ICMP (5)
    { fd_ioctl_midi },      // FD_TYPE_MIDI (6)
    { 0 },                  // FD_TYPE_NET (7)
    { 0 },                  // FD_TYPE_PIPE_R (8)
    { 0 },                  // FD_TYPE_PIPE_W (9)
    { 0 },                  // FD_TYPE_UDP (10)
    { fd_ioctl_vga },       // FD_TYPE_VGA (11)
};

// fd_table_base: return a pointer to the fd table inside the running
// program's program_state slot.  Every consumer reaches the table
// through this accessor so the right slot is used as the running
// program changes.  current_program_state is published as an asm
// label by entry.asm; the _g_current_program_state equ alias is
// emitted by signal.c.  program_state.h types fd_table as opaque
// bytes (struct fd is defined locally above and not visible to other
// includers); we cast back to ``struct fd *`` here.
struct fd *fd_table_base() {
    return (struct fd *)current_program_state->fd_table;
}

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
    cursor = fd_table_base();
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
        if ((entry->flags & O_WRONLY) != 0 && entry->dirty != 0) {
            vfs_update_size(entry);
        }
    } else if (entry->type == FD_TYPE_MIDI) {
        fd_close_midi();
    } else if (entry->type == FD_TYPE_PIPE_R || entry->type == FD_TYPE_PIPE_W) {
        fd_close_pipe(entry);
    }
    memset(entry, 0, FD_ENTRY_SIZE);
    return 1;
}

// fd_close_pipe — decrement the per-end refcount on close.  Wake the
// peer if its end fully closes so it can see EOF or EPIPE.  When both
// ends have closed the pool slot is freed.
void fd_close_pipe(struct fd *entry) {
    struct pipe *p;
    p = pipe_at(entry->start);
    if (p == 0) {
        // entry->start out of range — shouldn't happen if sys_pipeline2
        // always installs a valid pool index; fd_close's memset still
        // clears the slot after we return.
        return;
    }
    if (entry->type == FD_TYPE_PIPE_R) {
        pipe_decrement_reader(p);
        if (pipe_reader_open(p) == 0) {
            // No more readers — wake any writer parked on full buffer
            // so it sees EPIPE on its next attempt.
            pipe_wake_writer(p);
        }
    } else {
        pipe_decrement_writer(p);
        if (pipe_writer_open(p) == 0) {
            // No more writers — wake any reader parked on empty so it
            // drains the buffer and then sees EOF.
            pipe_wake_reader(p);
        }
    }
    if (pipe_both_ends_closed(p)) {
        pipe_release(p);
    }
}

// fd_dup: AX = new fd number (lowest free slot), CF set on error.
// Copies the source fd's entry to the new slot and resets dirty=0 on
// the destination.  Singleton-opener types (VGA/AUDIO/MIDI) refuse
// dup with ERROR_INVALID — their per-open state is exclusive.
__attribute__((carry_return))
int fd_dup(int *result __attribute__((out_register("ax"))),
           int old_fd __attribute__((in_register("bx")))) {
    struct fd *source;
    struct fd *destination;
    int new_fd;
    int i;
    if (!fd_lookup(old_fd, &source)) {
        *result = -1;
        return 0;
    }
    if (source->type == FD_TYPE_VGA || source->type == FD_TYPE_AUDIO || source->type == FD_TYPE_MIDI) {
        *result = -1;
        return 0;
    }
    destination = fd_table_base();
    i = 0;
    while (i < FD_MAX) {
        if (destination->type == FD_TYPE_FREE) {
            new_fd = i;
            break;
        }
        destination = destination + 1;
        i = i + 1;
    }
    if (i == FD_MAX) {
        *result = -1;
        return 0;
    }
    // Byte-copy the entry, then reset dirty on the destination.
    memcpy(destination, source, FD_ENTRY_SIZE);
    destination->dirty = 0;
    *result = new_fd;
    return 1;
}

// fd_dup2: copy old_fd's entry over target_fd's slot.  Closes whatever
// was at target first (respecting dirty).  If old == target, returns
// target unchanged (Linux semantics).  AX = target on success; CF set
// on error (bad old_fd, singleton-opener type, or out-of-range target).
__attribute__((carry_return))
int fd_dup2(int *result __attribute__((out_register("ax"))),
            int old_fd __attribute__((in_register("bx"))),
            int target_fd __attribute__((in_register("dx")))) {
    struct fd *source;
    struct fd *destination;
    if (!fd_lookup(old_fd, &source)) {
        *result = -1;
        return 0;
    }
    if (source->type == FD_TYPE_VGA || source->type == FD_TYPE_AUDIO || source->type == FD_TYPE_MIDI) {
        *result = -1;
        return 0;
    }
    if (target_fd < 0 || target_fd >= FD_MAX) {
        *result = -1;
        return 0;
    }
    if (old_fd == target_fd) {
        *result = target_fd;
        return 1;
    }
    // Close whatever was at target (no-op if free; flushes if needed).
    fd_close(target_fd);
    // fd_lookup the destination slot fresh — fd_close may have touched
    // it; we need a pointer to the now-free slot.
    destination = fd_table_base();
    destination = destination + target_fd;
    memcpy(destination, source, FD_ENTRY_SIZE);
    destination->dirty = 0;
    *result = target_fd;
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
// CF set if the fd is invalid.  PIPE_R/PIPE_W also return CF set —
// pipes have no file metadata; the fd is valid but unsupported here.
__attribute__((carry_return))
int fd_fstat(int *mode __attribute__((out_register("ax"))),
             int *size_high __attribute__((out_register("cx"))),
             int *size_low __attribute__((out_register("dx"))),
             int fd_num __attribute__((in_register("bx")))) {
    struct fd *entry;
    if (!fd_lookup(fd_num, &entry)) {
        return 0;
    }
    if (entry->type == FD_TYPE_PIPE_R || entry->type == FD_TYPE_PIPE_W) {
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
    memset(fd_table_base(), 0, FD_MAX * FD_ENTRY_SIZE);
    cursor = fd_table_base();
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
    cursor = fd_table_base();
    cursor = cursor + fd_num;
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
        cursor = fd_table_base();
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
        cursor = fd_table_base();
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
        entry->dirty = 1;  // truncate is a write that must flush size=0 on close
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

// fd_read_pipe — dequeue up to `count` bytes from the pipe's ring
// into the user buffer at EDI.  Blocks (via kernel_yield_read) when
// the buffer is empty and the write end is still open.  Returns 0
// (EOF) when the writer end is fully closed and the buffer is drained.
__attribute__((carry_return))
int fd_read_pipe(int *result __attribute__((out_register("ax"))),
                 struct fd *entry __attribute__((in_register("esi"))),
                 uint8_t *buffer __attribute__((in_register("edi"))),
                 int count __attribute__((in_register("ecx")))) {
    struct pipe *p;
    int bytes_read;
    p = pipe_at(entry->start);
    if (p == 0) {
        *result = -1;
        return 0;
    }
    while (1) {
        bytes_read = pipe_buffer_read(p, buffer, count);
        if (bytes_read > 0) {
            pipe_wake_writer(p);
            *result = bytes_read;
            return 1;
        }
        if (pipe_writer_open(p) == 0) {
            *result = 0;
            return 1;
        }
        kernel_yield_read(p);
        /* When kernel_yield_read returns, the scheduler has decided
           we're runnable again — try the read again. */
    }
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
    if (entry->type == FD_TYPE_FILE) {
        entry->dirty = 1;
    }
    __tail_call(handler, entry, count);
}

// fd_write_pipe — enqueue up to `count` bytes from fd_write_buffer
// into the pipe's ring.  Blocks (via kernel_yield_write) when the
// buffer is full and the read end is still open.  Returns -1 (EPIPE)
// when the reader end is fully closed and we still have bytes to
// write.  Otherwise returns the full `count` once all bytes are in.
__attribute__((carry_return))
int fd_write_pipe(int *result __attribute__((out_register("ax"))),
                  struct fd *entry __attribute__((in_register("esi"))),
                  int count __attribute__((in_register("ecx")))) {
    struct pipe *p;
    int bytes_written;
    int total;
    uint8_t *cursor;
    p = pipe_at(entry->start);
    if (p == 0) {
        *result = -1;
        return 0;
    }
    cursor = fd_write_buffer;
    total = 0;
    while (total < count) {
        if (pipe_reader_open(p) == 0) {
            *result = -1;
            return 0;
        }
        bytes_written = pipe_buffer_write(p, cursor, count - total);
        if (bytes_written > 0) {
            pipe_wake_reader(p);
            total = total + bytes_written;
            cursor = cursor + bytes_written;
        } else {
            kernel_yield_write(p);
            /* When kernel_yield_write returns, the scheduler has
               decided we're runnable — try the write again. */
        }
    }
    *result = total;
    return 1;
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
