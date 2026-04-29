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
