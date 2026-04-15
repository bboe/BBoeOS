# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

| Program | ASM (bytes) | C (bytes) | Delta |
|---------|-------------|-----------|-------|
| cat     | 138         | 138       |  0    |
| chmod   | 140         | 240       | +100  |
| cp      | 287         | 301       | +14   |
| date    | 15          | 15        |  0    |
| draw    | 245         | 282       | +37   |
| hello   | 22          | 23        | +1    |
| ls      | 129         | 193       | +64   |
| mkdir   | 116         | 121       | +5    |
| mv      | 232         | 276       | +44   |
| netinit | 72          | 72        |  0    |
| uptime  | 50          | 78        | +28   |

**chmod (+100):** The assembly version walks the argument with `lodsb`
(1 byte per character read); the C version reloads the base pointer
and indexes for each character check.

**cp (+14):** The BUILTIN_CLOBBERS correction forces the C version to
store the buffer pointer in memory and reload it across every
`read`/`write` call instead of pinning it to a register.

**draw (+37):** The assembly version keeps row/col packed in a single
DX register and edits it in place with `inc dh` / `dec dl`, then pokes
INT 10h for cursor moves, character output, and background palette.
The C version tracks each coordinate as a word-sized local and emits
state changes as a single `printf` of the full ANSI burst —
`\e[38;5;3m\e[48;5;%dm\e[%d;%dH*` — gated by a `changed` flag so
unmapped keypresses don't redraw.  Remaining overhead is the printf
call (push/call/cleanup around three args), the flag's store/test,
and the `dw 0` cells for each coordinate.

**hello (+1):** The C compiler emits a null terminator on every string
literal. The assembly version omits it since `FUNCTION_DIE` uses an
explicit length.

**ls (+64):** The assembly version uses inline `repne scasb` with a
25-byte cap to find the name length, then `FUNCTION_WRITE_STDOUT`
directly; the C version routes through `strlen()` (full 0xFFFF scan
setup) and `write(STDOUT, ...)` (full syscall path via BX=fd).  The
BUILTIN_CLOBBERS correction also forces the C version to spill the
entry pointer across `read`/`write` instead of pinning it to BX.

**mkdir (+5):** Same null-terminator overhead across 4 string literals
(+4 bytes), plus the compiler loads `argv` into AX before moving to
SI (+1 byte) rather than loading SI directly.

**mv (+44):** The assembly version walks the argument string once with
`lodsb` to both find the space separator and count newname length.
The C version calls `strlen(argv[1])` (which scans with `repne scasb`
plus setup/teardown), and reloads `argv` through BX for each indexed
access. Null terminators on 5 string literals add another +5.

**uptime (+28):** Uses `printf("%02d:%02d:%02d\n", ...)` which pushes
3 args and a format string onto the stack, calls `FUNCTION_PRINTF`,
and cleans up. The assembly version uses inline `FUNCTION_PRINT_DECIMAL`
calls with no stack overhead.
