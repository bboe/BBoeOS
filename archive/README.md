# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

The protected-mode merge made 32-bit the only production target for
user programs (`make_os.sh` passes `--bits 32`).  This archive is
mid-migration from 16-bit hand-written asm to 32-bit hand-written asm
so the comparison stays apples-to-apples.

- **ASM 16 (bytes)** — frozen historical 16-bit baseline; never
  changes once recorded.  Useful for spotting how much pmode (operand-
  size prefixes, dword pointer slots, wider PC-relative displacements)
  inflates each program.
- **ASM (bytes)** — current archive .asm assembled size.  Reflects
  whichever bits mode the file is in: rows whose `archive/<name>.asm`
  still has no `[bits 32]` directive read 16-bit; rows that have been
  re-written read 32-bit.  Once every row is converted this column
  will be uniformly 32-bit.
- **C (bytes)** — current `cc.py` output assembled size; matches the
  bits mode of the row's archive .asm (auto-detected by
  `tests/test_archive.py`).
- **Delta** — C − ASM.  The "where can cc.py do better" signal.

| Program | ASM 16 (bytes) | ASM (bytes) | C (bytes) | Delta |
|---------|----------------|-------------|-----------|-------|
| arp     | 466            | 466         | 469       | +3    |
| cat     | 145            | 175         | 181       | +6    |
| chmod   | 149            | 149         | 174       | +25   |
| cp      | 268            | 268         | 227       | -41   |
| date    | 15             | 21          | 21        |  0    |
| dns     | 724            | 724         | 1129      | +405  |
| hello   | 22             | 28          | 29        | +1    |
| ls      | 135            | 135         | 165       | +30   |
| mkdir   | 123            | 151         | 171       | +20   |
| mv      | 217            | 217         | 220       | +3    |
| netinit | 72             | 72          | 69        | -3    |
| netrecv | 334            | 334         | 403       | +69   |
| netsend | 187            | 187         | 221       | +34   |
| ping    | 1034           | 1034        | 1306      | +272  |
| shell   | 950            | 950         | 1337      | +387  |
| uptime  | 50             | 67          | 100       | +33   |

**arp (+3):** The three scratch arrays (`mac_buffer[6]`,
`receive_buffer[128]`, `target_ip[4]`) are file-scope BSS globals;
the assembly version uses inline `BUFFER`/`BUFFER+N` offsets.  The
remaining +3 is null terminators on the two `die()` strings.

**chmod (+25):** The assembly version walks the mode argument with
`lodsb` (1 byte per character read); the C version reloads the base
pointer and indexes for each character check.

**dns (+405):** All four buffers (`cname_buffer[128]`, `dns_ip[4]`,
`name_buffer[128]`, `query_buffer[512]`) are file-scope BSS globals;
the assembly version reused `SECTOR_BUFFER` and `BUFFER`.  Most of
the delta comes from `decode_domain` and `encode_domain` carrying
full stack-frame overhead (push bp / mov bp,sp / pop bp / ret per
call) and passing arguments via the stack (their callers pass complex
expressions, so the register calling convention doesn't kick in);
`skip_name` is simple enough that cc.py routes its arguments through
registers.  The assembly version uses register calling conventions
with no frame setup for every helper.  The C compiler also generates
word-sized loads with `xor ah,ah` zero-extension for every byte read,
whereas the assembly version uses `lodsb` / `stosb` / `rep movsb`
for compact byte-oriented loops.

**hello (+1):** The C compiler emits a null terminator on every string
literal. The assembly version omits it since `FUNCTION_DIE` uses an
explicit length.

**hello 16→32 (+6 ASM, +6 C):** Each `mov esi, imm` / `mov ecx, imm`
in 32-bit takes 5 bytes (one byte op + 4 bytes immediate) where the
16-bit form was 3 bytes (one byte op + 2 bytes immediate); the two
loads in main account for the +4, the wider PC-relative jump
displacement and the [bits 32] directive's effect on rel-jump
encoding contribute the rest.

**ls (+30):** The assembly version uses inline `repne scasb` with a
25-byte cap to find the name length, then `FUNCTION_WRITE_STDOUT`
directly; the C version routes through `strlen()` (full 0xFFFF scan
setup) and `write(STDOUT, ...)` (full syscall path via BX=fd).
`entry[DIRECTORY_ENTRY_SIZE]` is a local stack array, triggering a BP
frame for `main` (+6 bytes for prologue, +6 bytes for `lea`-vs-`mov`
addressing on `entry` accesses).

**mkdir (+20):** The asm version stores fd / pointer / count in
registers; cc.py 32-bit spills several into BP-relative locals
(visible in the prologue's wider ``sub esp, N``), and string-
literal handling adds a null terminator per string × 4 strings.
The 16-bit baseline's +4 was almost entirely null-terminator
overhead; the +20 here picks up cc.py's frame setup and 32-bit
prologue/epilogue cost too.

**netrecv (+69):** The C version uses stack-local `receive_buffer[128]`
and `mac_buffer[6]` in `main`'s BP frame where the assembly version
used `BUFFER + 128` and `BUFFER`.  The delta is otherwise ordinary
C-compiler overhead: null-terminated strings, the net_open CF
normalization, fd stashed in a memory local so it survives across
`FUNCTION_WRITE_STDOUT` calls, and printf-style hex formatting instead
of the asm version's inline `FUNCTION_PRINT_HEX` loop.

**netsend (+34):** Null terminators on three strings, the net_open
CF-to-integer normalization, and storing fd to a local all add a
handful of bytes.  The asm version kept fd in BX and used
length-bearing messages without null terminators.  The C version uses
a stack-local ``mac_buffer[6]`` in `main`'s BP frame; the asm version
uses ``BUFFER``.

**ping (+272):** Both versions build ICMP echo requests in userspace
over the same ``SYS_NET_OPEN (SOCK_DGRAM, IPPROTO_ICMP)`` /
``SYS_NET_SENDTO`` / ``SYS_NET_RECVFROM`` path.  The four scratch
arrays (``dns_ip[4]``, ``packet_buffer[128]``, ``query_buffer[512]``,
``target_ip[4]``) are local stack arrays in `main`; ``query_buffer``
and ``dns_ip`` are passed as parameters to ``resolve_dns`` so the
helper no longer reads file-scope globals.  Most of the delta is the
DNS fallback: ``encode_domain`` and ``resolve_dns`` carry full
stack-frame overhead (push bp / mov bp,sp / pop bp / ret) and pass
arguments via the stack — their call sites include complex
expressions so cc.py can't switch them to register passing.
``skip_name`` is simple enough that its two call sites now hand
arguments in registers.  The asm version inlines the equivalent
logic for every helper using register calling conventions.
Fixed-byte header layouts (DNS query header, QTYPE/QCLASS tail,
ICMP echo template) use ``memcpy`` from short string-literal
constants instead of per-byte assignments, which collapses each
~8 × ``mov byte [...], imm`` burst into a single ``rep movsb``.

**shell (+387):** The archived ``shell.asm`` has been edited so
that both versions share the same scratch layout — ``SECTOR_BUFFER
+ 4`` for the kill buffer and ``ARGV`` for the ``bin/<name>``
exec path — instead of carrying ~290 bytes of zero-initialized
trailing data inside the binary.  With storage out of the way,
the entire delta is pure code overhead.  Helper call overhead
(push bp / mov bp,sp / pop bp / ret, plus stack argument
passing on the cdecl-convention helpers ``cursor_back``,
``visual_bell``, ``insert_char``) is the bulk of it; the asm
version uses register-convention subroutines with local-label
jumps and inlines insert/delete.  cc.py does now route args into
registers for ``strcmp``, ``delete_at_cursor``, and ``try_exec``,
which matches the asm style for those three.  ``printf`` for
the ``ESC[nD`` cursor-back sequence routes each emission through
the full printf machinery (varargs push, format scan, specifier
dispatch) where the asm version emits bytes one at a time via
``putc``.  The ``if/else strcmp`` dispatch chain also runs
longer per comparison than the asm's ``dw string, handler``
table, and char locals spill to word slots so every byte load
comes with ``xor ah, ah`` zero-extension.

**uptime (+33):** Uses `printf("%02d:%02d:%02d\n", ...)` which pushes
3 args and a format string onto the stack, calls `FUNCTION_PRINTF`,
and cleans up. The assembly version uses inline `FUNCTION_PRINT_DECIMAL`
calls with no stack overhead.  The 32-bit asm rewrite drops the
``mov cl, 60 / div cl`` byte-divide trick (which left AH=remainder)
in favour of two uniform ``xor edx, edx / div ecx`` 32-bit divides
with EDX as remainder — slightly bigger but consistent.
