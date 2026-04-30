# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

The protected-mode merge made 32-bit the only production target for
user programs (`make_os.sh` passes `--bits 32`); every row in this
table is now 32-bit on both sides.

- **ASM 16 (bytes)** — frozen historical 16-bit baseline; never
  changes once recorded.  Useful for spotting how much pmode (operand-
  size prefixes, dword pointer slots, wider PC-relative displacements)
  inflates each program.
- **ASM (bytes)** — current 32-bit hand-written `archive/<name>.asm`
  assembled size.
- **C (bytes)** — current 32-bit `cc.py` output assembled size.
- **Delta** — C − ASM.  The "where can cc.py do better" signal.

| Program | ASM 16 (bytes) | ASM (bytes) | C (bytes) | Delta |
|---------|----------------|-------------|-----------|-------|
| arp     | 466            | 567         | 613       | +46   |
| cat     | 145            | 175         | 181       | +6    |
| chmod   | 149            | 175         | 228       | +53   |
| cp      | 268            | 338         | 302       | -36   |
| date    | 15             | 21          | 21        |  0    |
| dns     | 724            | 935         | 1437      | +502  |
| edit    | 2018           | 2668        | 3340      | +672  |
| hello   | 22             | 28          | 29        | +1    |
| ls      | 135            | 179         | 198       | +19   |
| mkdir   | 123            | 151         | 171       | +20   |
| mv      | 217            | 253         | 280       | +27   |
| netinit | 72             | 94          | 85        | -9    |
| netrecv | 334            | 424         | 452       | +28   |
| netsend | 187            | 215         | 255       | +40   |
| ping    | 1034           | 1238        | 1558      | +320  |
| shell   | 950            | 1189        | 1650      | +461  |
| uptime  | 50             | 67          | 100       | +33   |

**arp (+44):** The three scratch arrays (`mac_buffer[6]`,
`receive_buffer[128]`, `target_ip[4]`) are file-scope BSS globals;
the assembly version uses inline `BUFFER`/`BUFFER+N` offsets.  Two
structural wins from 32-bit: the 6-byte MAC copy (was 3× ``movsw``)
collapses to ``movsd + movsw``, and the sender-IP match (was two
2-byte ``mov ax, [..]; cmp ax, [..]``) collapses to a single 4-byte
compare.  Most of the +41 jump from the 16-bit +3 baseline is cc.py
gaining BP-frame setup overhead and ``movzx`` zero-extends on the
per-byte EtherType / opcode checks (each grows from a 5-byte
``cmp byte [],imm8`` to a wider zero-extend-then-compare pair).

**chmod (+53):** The assembly version walks the mode argument with
`lodsb` (1 byte per character read); the C version reloads the base
pointer and indexes for each character check.  The 32-bit asm widens
to 4-byte argv pointer slots (``[ARGV+4]`` was ``[ARGV+2]``); cc.py's
32-bit codegen for the per-character check sequence carries
proportionally more byte-load + zero-extend overhead than 16-bit, so
the delta inflates from +25 to +53.

**dns (+500):** All four buffers (`cname_buffer[128]`, `dns_ip[4]`,
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

The 32-bit asm widens dns_query.asm / encode_domain.asm helpers to
ESI/EDI plus dns_base / dns_socket_fd / domain_arg / rr_name_ptr to
``dd 0`` (cc.py 32-bit stores full EAX through them).  ``mov si, ax``
in decode_domain's pointer-resolution path becomes ``movzx eax, ax;
add eax, [dns_base]; mov esi, eax`` (the 16-bit form added a 16-bit
offset to a 16-bit base; the 32-bit form needs the zero-extend
because [dns_base] is now a 32-bit linear address).  Δ jumped from
+405 to +500 mostly because cc.py's 32-bit codegen pays the
``movzx`` zero-extend cost on every byte read in the answer-walking
loop, which the asm version still avoids.

**edit (+670):** Restored from git history (retired during the pmode
merge because the 16-bit C build couldn't represent a 256 KB buffer
base).  The 32-bit asm rewrites the gap buffer to live at
``EDIT_BUFFER_BASE = 0x100000`` (1 MB mark, past the VGA / BIOS
regions) with a 1 MB ``EDIT_BUFFER_SIZE`` and ``EDIT_KILL_BUFFER``
at the 2 MB mark — same layout the C version uses.  The 16-bit
baseline of 2018 bytes used a buffer floating on ``program_end``
inside segment 0 with a 25 KB cap.

The +670 delta is mostly two structural costs cc.py 32-bit pays
that the asm avoids: (1) every byte read from the gap buffer pays
``movzx ecx, byte [..]`` zero-extend instead of asm's bare ``mov al,
[..]``; (2) cc.py spills the ``cursor_column`` / ``cursor_line`` /
``view_*`` BSS globals through EAX for every increment / compare
where the asm hits memory directly.  The deep call graph (``move_*``,
``buf_*``, ``check_*``, ``do_*`` helpers) also pays for register
spilling cc.py's IR-based codegen does at every helper boundary.

**hello (+1):** The C compiler emits a null terminator on every string
literal. The assembly version omits it since `FUNCTION_DIE` uses an
explicit length.

**hello 16→32 (+6 ASM, +6 C):** Each `mov esi, imm` / `mov ecx, imm`
in 32-bit takes 5 bytes (one byte op + 4 bytes immediate) where the
16-bit form was 3 bytes (one byte op + 2 bytes immediate); the two
loads in main account for the +4, the wider PC-relative jump
displacement and the [bits 32] directive's effect on rel-jump
encoding contribute the rest.

**ls (+19):** The assembly version uses inline `repne scasb` with a
25-byte cap to find the name length, then `FUNCTION_WRITE_STDOUT`
directly; the C version routes through `strlen()` and
`write(STDOUT, ...)`.  `entry[DIRECTORY_ENTRY_SIZE]` is a local stack
array, triggering a BP frame for `main`.  Δ shrunk from +30 to +19
in 32-bit because the asm grew faster (+44 vs C's +31) — both the
``repne scasb`` walk-and-subtract and the ``test byte
[entry_buf+OFFSET]`` flag checks gain operand-size overhead in the
hand-written form, which narrows the compiler-vs-handwritten gap.
The +2 shift over the original +17 comes from cc.py preserving
``cld`` ahead of the C ``strlen``'s ``repne scasb``; the prefix used
to be stripped by ``peephole_unused_cld``, leaving DF undefined in
practice (typically clear via boot defaults).

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

**ping (+320):** Both versions build ICMP echo requests in userspace
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

**shell (+461):** The archived ``shell.asm`` reuses ``SECTOR_BUFFER
+ 4`` (private ``%assign``) for the kill buffer and ``ARGV`` for the
``bin/<name>`` exec path so it can avoid carrying ~290 bytes of
zero-initialized trailing data inside the binary.  The C version
keeps ``kill_buf[MAX_INPUT]`` in BSS (the live live SECTOR_BUFFER
fixed-address slot is no longer mapped in per-program PDs).  With
the storage difference accounted for, the rest of the delta is pure
code overhead.  Helper call overhead
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
