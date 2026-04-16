# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

| Program | ASM (bytes) | C (bytes) | Delta |
|---------|-------------|-----------|-------|
| cat     | 145         | 186       | +41   |
| chmod   | 149         | 240       | +91   |
| cp      | 268         | 291       | +23   |
| date    | 15          | 15        |  0    |
| draw    | 245         | 282       | +37   |
| hello   | 22          | 23        | +1    |
| ls      | 135         | 216       | +81   |
| mkdir   | 123         | 178       | +55   |
| mv      | 217         | 276       | +59   |
| netinit | 72          | 63        | -9    |
| netrecv | 332         | 416       | +84   |
| netsend | 185         | 223       | +38   |
| uptime  | 50          | 78        | +28   |

**cat (+41):** Both versions now use `FUNCTION_PARSE_ARGV`.  The C
version's overhead comes from the per-program `_argv: times 32 db 0`
buffer, `argv[0]` memory indirection through BX, and null terminators
on strings.

**chmod (+91):** The assembly version walks the mode argument with
`lodsb` (1 byte per character read); the C version reloads the base
pointer and indexes for each character check.

**cp (+23):** The BUILTIN_CLOBBERS correction forces the C version to
reload the buffer pointer across every `read`/`write` call instead
of pinning it to a register; the alias optimization shaves that
cost back down by emitting `mov di/si, SECTOR_BUFFER` directly in
place of the reloads.

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

**ls (+81):** The assembly version uses inline `repne scasb` with a
25-byte cap to find the name length, then `FUNCTION_WRITE_STDOUT`
directly; the C version routes through `strlen()` (full 0xFFFF scan
setup) and `write(STDOUT, ...)` (full syscall path via BX=fd).  The
BUILTIN_CLOBBERS correction also forces the C version to spill the
entry pointer across `read`/`write` instead of pinning it to BX.
The `argc/argv` startup adds further overhead from `FUNCTION_PARSE_ARGV`
and the 32-byte argv buffer.

**mkdir (+55):** The `argc/argv` startup adds `FUNCTION_PARSE_ARGV`
and a 32-byte argv buffer.  Null-terminator overhead across 4 string
literals adds another +4 bytes.

**mv (+59):** The C version calls `strlen(argv[1])` (which scans with
`repne scasb` plus setup/teardown) and reloads `argv` through BX for
each indexed access.  Both versions now use `FUNCTION_PARSE_ARGV`
for argument splitting; the remaining delta is the per-program argv
buffer and null terminators on 5 string literals.

**netrecv (+84):** Both versions read into `BUFFER + 128` with a
capped 128-byte read -- plenty for the ARP reply that's being demoed.
The delta is ordinary C-compiler overhead: null-terminated strings,
the net_open CF normalization, fd stashed in a memory local so it
survives across `FUNCTION_WRITE_STDOUT` calls, and printf-style hex
formatting instead of the asm version's inline `FUNCTION_PRINT_HEX`
loop.

**netsend (+38):** Null terminators on three strings, the net_open
CF-to-integer normalization, and storing fd to a local all add a
handful of bytes.  The asm version kept fd in BX and used
length-bearing messages without null terminators.  Both versions
stash the MAC in the shell's idle input buffer at ``BUFFER`` rather
than in an embedded cell.

**uptime (+28):** Uses `printf("%02d:%02d:%02d\n", ...)` which pushes
3 args and a format string onto the stack, calls `FUNCTION_PRINTF`,
and cleans up. The assembly version uses inline `FUNCTION_PRINT_DECIMAL`
calls with no stack overhead.
