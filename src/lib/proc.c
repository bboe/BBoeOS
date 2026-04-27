// proc.c — shared library functions for user programs (called via the
// FUNCTION_* jump table installed at fixed kernel addresses).  Lives in
// the kernel image alongside drivers/ / fs/ / net/.
//
// shared_exit / shared_die / shared_get_character are thin INT 30h
// syscall thunks — autodetected by cc.py as naked_asm (no parameters,
// single asm() body); body emitted verbatim with an auto-appended ret.
// shared_parse_argv is a real C function: the uint8_t** parameter
// landed in PR #223, and out_register("cx") gives the asm-side caller
// (FUNCTION_PARSE_ARGV) its expected CX = argc return convention.

uint8_t *exec_arg_pointer __attribute__((asm_name("EXEC_ARG")));

void shared_exit() {
    // SYS_EXIT — kernel reloads the shell; never returns.
    asm("mov ah, SYS_EXIT\n"
        "int 30h");
}

void shared_die() {
    // Write CX bytes from SI to stdout, then exit.  ``jmp shared_exit``
    // reaches the body of the function emitted directly above; the asm
    // version relied on label fall-through which isn't expressible in C.
    asm("call shared_write_stdout\n"
        "jmp shared_exit");
}

void shared_get_character() {
    // Read one byte from stdin via SYS_IO_READ.  Returns AL = byte read.
    // BX / CX / DI saved/restored so the caller's pinned values survive.
    asm("push bx\n"
        "push cx\n"
        "push di\n"
        "mov bx, STDIN\n"
        "mov di, SECTOR_BUFFER\n"
        "mov cx, 1\n"
        "mov ah, SYS_IO_READ\n"
        "int 30h\n"
        "pop di\n"
        "pop cx\n"
        "pop bx\n"
        "mov al, [SECTOR_BUFFER]");
}

// Split [EXEC_ARG] at spaces into an argv-style pointer array.
//   Input:  argv (DI) — caller-provided pointer buffer
//   Output: *argc (CX via out_register) — number of arguments
// Walks the argv string, records each whitespace-delimited word's start
// in argv[count], and null-terminates each word in place.  An empty or
// NULL [EXEC_ARG] yields count = 0.
void shared_parse_argv(uint8_t **argv __attribute__((in_register("di"))),
                       int *argc __attribute__((out_register("cx")))) {
    int count;
    uint8_t *str;
    count = 0;
    str = exec_arg_pointer;
    if (str != 0) {
        while (1) {
            while (str[0] == ' ') {
                str = str + 1;
            }
            if (str[0] == 0) {
                break;
            }
            argv[count] = str;
            count = count + 1;
            while (1) {
                if (str[0] == 0) {
                    break;
                }
                if (str[0] == ' ') {
                    break;
                }
                str = str + 1;
            }
            if (str[0] == 0) {
                break;
            }
            str[0] = 0;
            str = str + 1;
        }
    }
    *argc = count;
}
