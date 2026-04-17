# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

| Program | ASM (bytes) | C (bytes) | Delta |
|---------|-------------|-----------|-------|
| arp     | 451         | 455       | +4    |
| cat     | 145         | 135       | -10   |
| chmod   | 149         | 173       | +24   |
| cp      | 268         | 236       | -32   |
| date    | 15          | 15        |  0    |
| dns     | 722         | 1193      | +471  |
| draw    | 245         | 282       | +37   |
| hello   | 22          | 23        | +1    |
| ls      | 135         | 173       | +38   |
| mkdir   | 123         | 127       | +4    |
| mv      | 217         | 217       |  0    |
| netinit | 72          | 63        | -9    |
| netrecv | 334         | 384       | +50   |
| netsend | 187         | 214       | +27   |
| uptime  | 50          | 78        | +28   |

**arp (+4):** Null terminators on 4 strings (+4 bytes).  The
remaining code is byte-identical to the hand-written assembly.

**chmod (+24):** The assembly version walks the mode argument with
`lodsb` (1 byte per character read); the C version reloads the base
pointer and indexes for each character check.

**dns (+471):** Both versions use the same shared memory regions
(`SECTOR_BUFFER` for the query/response, `BUFFER` for name decoding).
The C version is larger because the helper functions (`decode_domain`,
`encode_domain`, `skip_name`) carry full stack-frame overhead (push bp /
mov bp,sp / pop bp / ret per call) and pass arguments via the stack,
while the assembly version uses register calling conventions with no
frame setup.  The C compiler also generates word-sized loads with `xor
ah,ah` zero-extension for every byte read, whereas the assembly version
uses `lodsb` / `stosb` / `rep movsb` for compact byte-oriented loops.

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

**ls (+38):** The assembly version uses inline `repne scasb` with a
25-byte cap to find the name length, then `FUNCTION_WRITE_STDOUT`
directly; the C version routes through `strlen()` (full 0xFFFF scan
setup) and `write(STDOUT, ...)` (full syscall path via BX=fd).

**mkdir (+4):** Null-terminator overhead across 4 string literals.

**netrecv (+50):** Both versions read into `BUFFER + 128` with a
capped 128-byte read -- plenty for the ARP reply that's being demoed.
The delta is ordinary C-compiler overhead: null-terminated strings,
the net_open CF normalization, fd stashed in a memory local so it
survives across `FUNCTION_WRITE_STDOUT` calls, and printf-style hex
formatting instead of the asm version's inline `FUNCTION_PRINT_HEX`
loop.

**netsend (+27):** Null terminators on three strings, the net_open
CF-to-integer normalization, and storing fd to a local all add a
handful of bytes.  The asm version kept fd in BX and used
length-bearing messages without null terminators.  Both versions
stash the MAC in the shell's idle input buffer at ``BUFFER`` rather
than in an embedded cell.

**uptime (+28):** Uses `printf("%02d:%02d:%02d\n", ...)` which pushes
3 args and a format string onto the stack, calls `FUNCTION_PRINTF`,
and cleans up. The assembly version uses inline `FUNCTION_PRINT_DECIMAL`
calls with no stack overhead.
