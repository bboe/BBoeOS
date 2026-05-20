        .intel_syntax noprefix
        .text
        .globl _start

/* The kernel hands control to PROGRAM_BASE with:
 *   - ESP = USER_STACK_TOP (0xFF800000)
 *   - The handoff frame at user-virt 0x1000 contains EXEC_ARG (the
 *     program's argument string pointer at 0x14FC) and BUFFER (0x1500)
 *   - x87 FPU enabled (CR0.MP|NE set, CR0.EM clear); we still FNINIT
 *     to reset state.
 *
 * argv is a stub — argc=1, argv={"", NULL} — so main() sees a
 * well-formed but empty argv.  The shell stores the typed command
 * name at BUFFER (user-virt 0x1500); a future change can wire that
 * through if a program needs to read its own name.
 *
 * The linker script (tools/libc/program.ld) provides __bss_start and
 * __bss_end; we zero that range before calling main. */
_start:
        fninit

        cld
        lea  edi, [__bss_start]
        lea  ecx, [__bss_end]
        sub  ecx, edi
        shr  ecx, 2
        xor  eax, eax
        rep  stosd

        lea  eax, [_argv]
        push eax
        push 1
        call main
        add  esp, 8

        push eax
        call exit
1:      hlt
        jmp  1b

        .data
_argv:  .long _argv_str
        .long 0
_argv_str: .asciz ""
