# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

| Program | ASM (bytes) | C (bytes) | Delta |
|---------|-------------|-----------|-------|
| arp     | 451         | 457       | +6    |
| cat     | 145         | 135       | -10   |
| chmod   | 149         | 173       | +24   |
| cp      | 268         | 236       | -32   |
| date    | 15          | 15        |  0    |
| dns     | 724         | 1116      | +392  |
| draw    | 245         | 265       | +20   |
| edit    | 1977        | 2587      | +610  |
| hello   | 22          | 23        | +1    |
| ls      | 135         | 168       | +33   |
| mkdir   | 123         | 127       | +4    |
| mv      | 217         | 217       |  0    |
| netinit | 72          | 63        | -9    |
| netrecv | 334         | 380       | +46   |
| netsend | 187         | 216       | +29   |
| ping    | 1019        | 1217      | +198  |
| shell   | 921         | 1358      | +437  |
| uptime  | 50          | 78        | +28   |

**arp (+4):** Null terminators on 4 strings (+4 bytes).  The
remaining code is byte-identical to the hand-written assembly.

**chmod (+24):** The assembly version walks the mode argument with
`lodsb` (1 byte per character read); the C version reloads the base
pointer and indexes for each character check.

**dns (+396):** Both versions use the same shared memory regions
(`SECTOR_BUFFER` for the query/response, `BUFFER` for name decoding).
The C version is larger because the helper functions (`decode_domain`,
`encode_domain`, `skip_name`) carry full stack-frame overhead (push bp /
mov bp,sp / pop bp / ret per call) and pass arguments via the stack,
while the assembly version uses register calling conventions with no
frame setup.  The C compiler also generates word-sized loads with `xor
ah,ah` zero-extension for every byte read, whereas the assembly version
uses `lodsb` / `stosb` / `rep movsb` for compact byte-oriented loops.

**edit (+611):** Both versions implement the same gap-buffer /
kill-buffer editor over the same key bindings.  The C version
translates `ESC [ A/B/C/D` into the matching Ctrl-char before
dispatching, so arrow keys and Ctrl+B/F/N/P share a single move
body — same trick the asm achieves via fall-through to local
labels.  `buffer_character_at` is still a real function call
(frame setup, stack arguments, ret) invoked from render's inner
loop and from save; the asm inlines the equivalent logic with
register-convention subroutines.  Cursor repositioning uses
`printf("\e[%d;%dH", ...)` (varargs push / format scan / `add
sp, 6`) where the asm emits a literal ESC sequence through
`FUNCTION_PRINT_CHARACTER`.  char locals spill to word slots so
every byte read comes with a `xor ah, ah` zero-extension.

**hello (+1):** The C compiler emits a null terminator on every string
literal. The assembly version omits it since `FUNCTION_DIE` uses an
explicit length.

**ls (+36):** The assembly version uses inline `repne scasb` with a
25-byte cap to find the name length, then `FUNCTION_WRITE_STDOUT`
directly; the C version routes through `strlen()` (full 0xFFFF scan
setup) and `write(STDOUT, ...)` (full syscall path via BX=fd).

**mkdir (+4):** Null-terminator overhead across 4 string literals.

**netrecv (+46):** Both versions read into `BUFFER + 128` with a
capped 128-byte read -- plenty for the ARP reply that's being demoed.
The delta is ordinary C-compiler overhead: null-terminated strings,
the net_open CF normalization, fd stashed in a memory local so it
survives across `FUNCTION_WRITE_STDOUT` calls, and printf-style hex
formatting instead of the asm version's inline `FUNCTION_PRINT_HEX`
loop.

**netsend (+29):** Null terminators on three strings, the net_open
CF-to-integer normalization, and storing fd to a local all add a
handful of bytes.  The asm version kept fd in BX and used
length-bearing messages without null terminators.  Both versions
stash the MAC in the shell's idle input buffer at ``BUFFER`` rather
than in an embedded cell.

**ping (+198):** Both versions build ICMP echo requests in userspace
over the same ``SYS_NET_OPEN (SOCK_DGRAM, IPPROTO_ICMP)`` /
``SYS_NET_SENDTO`` / ``SYS_NET_RECVFROM`` path.  Most of the delta is
the DNS fallback: ``encode_domain``, ``skip_name``, and
``resolve_dns`` each carry full stack-frame overhead (push bp /
mov bp,sp / pop bp / ret) and pass arguments via the stack, whereas
the asm version inlines the equivalent logic using register
calling conventions.  Fixed-byte header layouts (DNS query header,
QTYPE/QCLASS tail, ICMP echo template) use ``memcpy`` from short
string-literal constants instead of per-byte assignments, which
collapses each ~8 × ``mov byte [...], imm`` burst into a single
``rep movsb``.

**shell (+437):** The archived ``shell.asm`` has been edited so
that both versions share the same scratch layout — ``SECTOR_BUFFER
+ 4`` for the kill buffer and ``ARGV`` for the ``bin/<name>``
exec path — instead of carrying ~290 bytes of zero-initialized
trailing data inside the binary.  With storage out of the way,
the entire delta is pure code overhead.  cdecl call setup (push
bp / mov bp,sp / pop bp / ret, plus stack argument passing) on
every helper — ``streq``, ``cursor_back``, ``visual_bell``,
``insert_char``, ``delete_at_cursor``, ``try_exec`` — is the
bulk of it; the asm version uses register-convention subroutines
with local-label jumps and inlines insert/delete.  ``printf`` for
the ``ESC[nD`` cursor-back sequence routes each emission through
the full printf machinery (varargs push, format scan, specifier
dispatch) where the asm version emits bytes one at a time via
``putc``.  The ``if/else streq`` dispatch chain also runs longer
per comparison than the asm's ``dw string, handler`` table, and
char locals spill to word slots so every byte load comes with
``xor ah, ah`` zero-extension.

**uptime (+28):** Uses `printf("%02d:%02d:%02d\n", ...)` which pushes
3 args and a format string onto the stack, calls `FUNCTION_PRINTF`,
and cleans up. The assembly version uses inline `FUNCTION_PRINT_DECIMAL`
calls with no stack overhead.
