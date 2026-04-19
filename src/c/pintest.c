/* Smoke test for cc.py's ``__attribute__((asm_register("si")))`` global
   aliasing.  The declared global ``cursor`` shares storage with the SI
   register — reads compile as ``mov ax, si`` (or skip the move entirely
   when SI is already in the target register), writes as ``mov si, ...``
   — and no ``_g_cursor`` memory slot is emitted at the binary tail.
   Exercises:
     - assignment from a char-array global (``cursor = source``)
     - constant-index byte read (``cursor[0]``)
     - pointer increment (``cursor = cursor + 1``) through a while loop
     - final indirect read prints through the variable */

__attribute__((asm_register("si")))
char *cursor;

char source[] = {' ', ' ', ' ', 'h', 'e', 'l', 'l', 'o', '\0'};

int main() {
    cursor = source;
    while (cursor[0] == ' ') {
        cursor = cursor + 1;
    }
    printf("first non-space: %c\n", cursor[0]);  /* h */
    return 0;
}
