/* fault_test — consolidated user-fault and bad-pointer tests.
 *
 * Three modes that each exercise a kernel-side defensive path that
 * used to be its own program:
 *
 *   null         — write to virtual address 0 (unmapped); kernel raises
 *                  #PF in CPL=3, tears down the PD, re-enters
 *                  shell_reload.  Previously nullderef.c.
 *
 *   gp           — `cli` at CPL=3 with IOPL=0 raises #GP; same kill
 *                  path through idt.asm's exc_common.  Previously
 *                  gptest.c.
 *
 *   kernel_buf   — SYS_IO_WRITE with a kernel-half buffer pointer
 *                  (KERNEL_VIRT_BASE).  Tests access_ok validation:
 *                  the syscall must return CF=1 / EAX < 0 with no #PF
 *                  on the serial console.  Previously okptest.c. */

int saw_cf;
int result;

/* Forward declarations — clang requires them since main() is sorted
   alphabetically and lands ahead of every callee it dispatches to.
   cc.py's whole-file pre-pass resolves these without prototypes. */
void mode_gp();
void mode_kernel_buf();
void mode_null();
int string_equal(char *left, char *right);

int main(int argc, char *argv[]) {
    if (argc < 2) {
        die("fault_test: pass a mode\n");
    }
    char *mode = argv[1];
    if (string_equal(mode, "null")) {
        mode_null();
    } else if (string_equal(mode, "gp")) {
        mode_gp();
    } else if (string_equal(mode, "kernel_buf")) {
        mode_kernel_buf();
    } else {
        die("fault_test: unknown mode\n");
    }
    return 0;
}

void mode_gp() {
    printf("gptest: cli from ring 3\n");
    asm("cli");
    printf("unreachable: kill path failed\n");
}

void mode_kernel_buf() {
    printf("okptest: io_write to kernel-half pointer\n");
    /* The inline asm avoids ``setc`` because the self-hosted assembler
       in user/programs/asm.c hasn't grown setcc support yet — capturing CF via
       a ``jnc`` branch keeps this assemblable by both NASM and the
       self-host. */
    asm("mov ebx, 1\n"                /* fd = STDOUT */
        "mov esi, KERNEL_VIRT_BASE\n" /* bad user pointer */
        "mov ecx, 16\n"
        "mov ah, SYS_IO_WRITE\n"
        "int 30h\n"
        "mov [_g_result], eax\n"
        "mov eax, 0\n"
        "jnc .fault_test_no_cf\n"
        "mov eax, 1\n"
        ".fault_test_no_cf:\n"
        "mov [_g_saw_cf], eax\n");
    if (saw_cf == 0) {
        printf("fail: CF=0\n");
        return;
    }
    if (result >= 0) {
        printf("fail: result=%d (expected < 0)\n", result);
        return;
    }
    printf("ok: bad pointer rejected\n");
}

void mode_null() {
    printf("nullderef: writing to NULL\n");
    /* cc.py rejects ``*(int *)0 = 42``; the inline asm produces the
       same encoding the cast would. */
    asm("mov dword [0], 42");
    printf("unreachable: kill path failed\n");
}

int string_equal(char *left, char *right) {
    int index = 0;
    while (left[index] != '\0' && right[index] != '\0') {
        if (left[index] != right[index]) {
            return 0;
        }
        index = index + 1;
    }
    return left[index] == right[index];
}
