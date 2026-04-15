# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

| Program | ASM (bytes) | C (bytes) | Delta |
|---------|-------------|-----------|-------|
| cat     | 138         | 121       | -17   |
| chmod   | 140         | 241       | +101  |
| cp      | 287         | 285       | -2    |
| date    | 15          | 15        |  0    |
| draw    | 238         | 381       | +143  |
| hello   | 22          | 23        | +1    |
| ls      | 129         | 170       | +41   |
| mkdir   | 116         | 121       | +5    |
| mv      | 232         | 277       | +45   |
| uptime  | 50          | 78        | +28   |

**chmod (+101):** The assembly version walks the argument with `lodsb`
(1 byte per character read); the C version reloads the base pointer
and indexes for each character check.

**draw (+143):** The assembly version keeps row/col packed in a single
DX register and edits it in place with `inc dh` / `dec dl`, then pokes
INT 10h for cursor moves, character output, and background palette.
The C version tracks each coordinate as a word-sized local, recomputes
wrap boundaries with explicit comparisons, and emits every cursor move
and color change as an ANSI escape through `printf` — which costs the
format string, a runtime printf call, and `add sp` cleanup around
every update.  Locals also spill to memory since the printf/getc
calls clobber the auto-pin register pool.

**hello (+1):** The C compiler emits a null terminator on every string
literal. The assembly version omits it since `FUNCTION_DIE` uses an
explicit length.

**ls (+41):** The assembly version uses inline `repne scasb` with a
25-byte cap to find the name length, then `FUNCTION_WRITE_STDOUT`
directly; the C version routes through `strlen()` (full 0xFFFF scan
setup) and `write(STDOUT, ...)` (full syscall path via BX=fd).

**mkdir (+5):** Same null-terminator overhead across 4 string literals
(+4 bytes), plus the compiler loads `argv` into AX before moving to
SI (+1 byte) rather than loading SI directly.

**mv (+45):** The assembly version walks the argument string once with
`lodsb` to both find the space separator and count newname length.
The C version calls `strlen(argv[1])` (which scans with `repne scasb`
plus setup/teardown), and reloads `argv` through BX for each indexed
access. Null terminators on 5 string literals add another +5.

**uptime (+28):** Uses `printf("%02d:%02d:%02d\n", ...)` which pushes
3 args and a format string onto the stack, calls `FUNCTION_PRINTF`,
and cleans up. The assembly version uses inline `FUNCTION_PRINT_DECIMAL`
calls with no stack overhead.
