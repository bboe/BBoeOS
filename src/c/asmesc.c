/* Smoke test for cc.py's inline-asm escape.  Exercises both forms:
   file-scope ``asm(...)`` to plant a byte table at a fixed label,
   then a statement-level ``asm(...)`` that reads the table into a
   global — verifying the two escapes see the same symbol table that
   the surrounding C code does. */

asm("asmesc_table: db 42, 99, 7, 11");

int value;

int main() {
    asm("mov bx, asmesc_table\nmov al, [bx+2]\nxor ah, ah\nmov [_g_value], ax");
    printf("value = %u\n", value);  /* 7 */
    return 0;
}
