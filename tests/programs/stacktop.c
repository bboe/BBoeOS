/* Verify USER_STACK_TOP lives at the high address right at the
   user/kernel boundary (USER_STACK_TOP = KERNEL_VIRT_BASE, currently
   0xFF800000).  Captures the top byte of ESP on entry and prints
   it.  At iretd ESP equals USER_STACK_TOP, so the high byte is
   0xff — anything else (e.g. 0xc0 from the prior lift, or 0x40 from
   the original layout) means the constants drifted out of sync.
   cc.py emits no main-prologue for this program, so the very first
   instruction reads ESP directly. */

int esp_high;

int main() {
    asm("mov eax, esp\nshr eax, 24\nmov [_g_esp_high], eax");
    printf("stacktop: high=%x\n", esp_high);
    return 0;
}
