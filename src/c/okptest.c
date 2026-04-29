/* Smoke test for the access_ok pointer validation in syscall handlers.
   Calls SYS_IO_WRITE with a kernel-half buffer pointer (KERNEL_VIRT_BASE
   = 0xC0000000) and asserts: the kernel does NOT take a #PF (no EXC0E
   on the serial console), the syscall returns CF=1, and EAX is < 0.
   Pairs with the okptest entry in tests/test_programs.py.

   The inline asm avoids ``setc`` because the self-hosted assembler
   in src/c/asm.c hasn't grown setcc support yet — capturing CF via
   a ``jnc`` branch keeps okptest assemblable by both NASM and the
   self-host (and so keeps tests/test_asm.py green). */

int saw_cf;
int result;

int main() {
    printf("okptest: io_write to kernel-half pointer\n");
    asm("mov ebx, 1\n"                                  /* fd = STDOUT */
        "mov esi, KERNEL_VIRT_BASE\n"                   /* bad user pointer */
        "mov ecx, 16\n"
        "mov ah, SYS_IO_WRITE\n"
        "int 30h\n"
        "mov [_g_result], eax\n"
        "mov eax, 0\n"
        "jnc .okptest_no_cf\n"
        "mov eax, 1\n"
        ".okptest_no_cf:\n"
        "mov [_g_saw_cf], eax\n");
    if (saw_cf == 0) {
        printf("fail: CF=0\n");
        return 1;
    }
    if (result >= 0) {
        printf("fail: result=%d (expected < 0)\n", result);
        return 1;
    }
    printf("ok: bad pointer rejected\n");
    return 0;
}
