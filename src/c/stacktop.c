/* Verify USER_STACK_TOP lives at the new high address (0xC0000000,
   right at the user/kernel boundary).  Captures the top byte of ESP
   on entry and prints it.  At iretd ESP equals USER_STACK_TOP, so
   the high byte is 0xC0 — anything else (e.g. 0x40 from the
   pre-lift layout) means the lift didn't take effect.  cc.py emits
   no main-prologue for this program, so the very first instruction
   reads ESP directly. */

int esp_high;

int main() {
    asm("mov eax, esp\nshr eax, 24\nmov [_g_esp_high], eax");
    printf("stacktop: high=%x\n", esp_high);
    return 0;
}
