# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

| Program | ASM (bytes) | C (bytes) | Delta |
|---------|-------------|-----------|-------|
| cat     | 138         | 117       | -21   |
| chmod   | 140         | 246       | +106  |
| date    | 74          | 72        | -2    |
| hello   | 22          | 23        | +1    |
| mkdir   | 116         | 121       | +5    |
| uptime  | 50          | 49        | -1    |

**chmod (+106):** The assembly version walks the argument with `lodsb`
(1 byte per character read); the C version reloads the base pointer
and indexes for each character check.

**hello (+1):** The C compiler emits a null terminator on every string
literal. The assembly version omits it since `FUNCTION_DIE` uses an
explicit length.

**mkdir (+5):** Same null-terminator overhead across 4 string literals
(+4 bytes), plus the compiler loads `argv` into AX before moving to
SI (+1 byte) rather than loading SI directly.
