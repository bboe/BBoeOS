// fd/console.c — read/write implementations for FD_TYPE_CONSOLE.
// Dispatched via fd_ops in fs/fd.c when the syscall layer hands a
// console-typed fd to fd_read / fd_write.
//
// fd_read_console returns at most one byte per call regardless of the
// caller's `count` — the shell wants a line-discipline-style stream
// (read one byte, decide whether to loop) and the asm version's
// behaviour is the same.  CR (0x0D) is translated to LF (0x0A) on
// input so PS/2 Enter scancodes (which decode as CR via ps2.c's
// keymap) and serial-terminal Enter (which sends CR) both surface as
// Unix-style line endings.  put_character on the output path already
// translates LF → CRLF, so the symmetry is clean.

// drivers/ps2.c — non-blocking read; returns 0 with ZF set if the
// keyboard ring buffer is empty.
char ps2_getc();

// fd_ioctl_console: non-blocking peek/get of one byte (or one event)
// for FD_TYPE_CONSOLE.  Cmd byte (AL) selects the operation:
//   CONSOLE_IOCTL_TRY_GETC      (0) — AX = ASCII byte (0 if empty)
//   CONSOLE_IOCTL_TRY_GET_EVENT (1) — EAX = (pressed<<16)|bbkey (0 if empty)
// Returns CF clear on success, CF set for unknown cmd.  Stays as inline
// asm because the syscall jump-table dispatch enters with a
// register-state contract (AL=cmd, ESI=entry) that cc.py's
// prologue/epilogue would clobber.
//
// TRY_GET_EVENT pops from this fd's inline event ring (head/tail at
// FD_OFFSET_EVENT_HEAD/TAIL, slots at FD_OFFSET_EVENT_BUF) — Linux's
// per-fd evdev model.  Producer (drivers/ps2.c ps2_broadcast_event)
// pushes from IRQ context to every readable console fd; consumer
// (this) drains only the calling fd, so independent readers don't
// steal each other's events.  Wire format and BBKEY_* code list:
// tools/libc/include/bbkeys.h.
void fd_ioctl_console();

asm("fd_ioctl_console:\n"
    "        cmp al, 0x00\n"                    // CONSOLE_IOCTL_TRY_GETC
    "        je .fd_ioctl_console_try_getc\n"
    "        cmp al, 0x01\n"                    // CONSOLE_IOCTL_TRY_GET_EVENT
    "        je .fd_ioctl_console_try_get_event\n"
    "        stc\n"
    "        ret\n"

    ".fd_ioctl_console_try_getc:\n"
    "        sti\n"                              // let IRQ 1 fire so the PS/2 ring populates
    "        call ps2_getc\n"                    // AL = char or 0
    "        movzx eax, al\n"
    "        test al, al\n"
    "        jnz .fd_ioctl_console_done\n"
    // PS/2 ring empty — try the serial LSR (port 0x3FD bit 0 = data ready).
    "        push edx\n"
    "        mov dx, 0x3FD\n"
    "        in al, dx\n"
    "        test al, 0x01\n"
    "        jz .fd_ioctl_console_serial_empty\n"
    "        mov dx, 0x3F8\n"
    "        in al, dx\n"
    "        movzx eax, al\n"
    "        pop edx\n"
    "        jmp .fd_ioctl_console_done\n"
    ".fd_ioctl_console_serial_empty:\n"
    "        pop edx\n"
    "        xor eax, eax\n"
    "        jmp .fd_ioctl_console_done\n"

    ".fd_ioctl_console_try_get_event:\n"
    // Per-fd ring drain at [esi + FD_OFFSET_EVENT_BUF].  ESI = fd
    // entry pointer (set by fd_ioctl).  Serial doesn't carry release
    // events so the empty-queue path falls back to a synthesized
    // make-only event from the serial LSR.
    "        sti\n"
    "        push ebx\n"
    "        movzx eax, byte [esi + FD_OFFSET_EVENT_HEAD]\n"
    "        cmp al, [esi + FD_OFFSET_EVENT_TAIL]\n"
    "        je .fd_ioctl_console_event_empty\n"
    "        movzx ebx, al\n"
    "        mov eax, [esi + FD_OFFSET_EVENT_BUF + ebx*4]\n"
    "        inc bl\n"
    "        and bl, FD_EVENT_QUEUE_LEN - 1\n"
    "        mov [esi + FD_OFFSET_EVENT_HEAD], bl\n"
    "        pop ebx\n"
    "        jmp .fd_ioctl_console_done\n"
    ".fd_ioctl_console_event_empty:\n"
    "        pop ebx\n"
    "        push edx\n"
    "        mov dx, 0x3FD\n"
    "        in al, dx\n"
    "        test al, 0x01\n"
    "        jz .fd_ioctl_console_serial_empty_event\n"
    "        mov dx, 0x3F8\n"
    "        in al, dx\n"
    "        movzx eax, al\n"
    "        or eax, 0x100\n"                    // pressed=1
    "        pop edx\n"
    "        jmp .fd_ioctl_console_done\n"
    ".fd_ioctl_console_serial_empty_event:\n"
    "        pop edx\n"
    "        xor eax, eax\n"

    ".fd_ioctl_console_done:\n"
    "        clc\n"
    "        ret");

// drivers/console.c — single-byte console output (handles ANSI parsing
// + serial mirror + screen write).  Preserves all caller registers.
void put_character(char byte __attribute__((in_register("ax"))))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
    __attribute__((preserve_register("esi")));

// fs/fd.c file-scope global; the dispatcher (fd_write) stashes the
// user buffer pointer here before jumping to this handler.
extern uint8_t *fd_write_buffer;

// Read one byte from PS/2 ring or COM1 into *destination.  Always
// returns CF clear; AX = 1 on success, 0 if max_bytes was 0.  Polls
// continuously — the syscall handler entered with IF=0 (the INT 30h
// gate clears it) so we sti once before the polling loop to let
// IRQ 1 fire and the keyboard ring populate.
__attribute__((carry_return))
int fd_read_console(int *bytes_read __attribute__((out_register("ax"))),
                    uint8_t *destination __attribute__((in_register("edi"))),
                    int max_bytes __attribute__((in_register("ecx")))) {
    char byte;
    if (max_bytes == 0) {
        *bytes_read = 0;
        return 1;
    }
    asm("sti");
    while (1) {
        byte = ps2_getc();
        if (byte != '\0') {
            break;
        }
        if ((kernel_inb(0x3FD) & 0x01) != 0) {
            byte = kernel_inb(0x3F8);
            break;
        }
    }
    if (byte == '\r') {
        byte = '\n';
    }
    destination[0] = byte;
    *bytes_read = 1;
    return 1;
}

// Write `count` bytes from fd_write_buffer through put_character (which
// handles ANSI parsing + serial mirror + screen write).  Always returns
// CF clear; AX = bytes written = count.
__attribute__((carry_return))
int fd_write_console(int *bytes_written __attribute__((out_register("ax"))),
                     int count __attribute__((in_register("ecx")))) {
    int index;
    index = 0;
    while (index < count) {
        put_character(fd_write_buffer[index]);
        index = index + 1;
    }
    *bytes_written = count;
    return 1;
}
