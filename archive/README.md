# Archive

Assembly programs that have been rewritten in C. The original assembly
source is kept here for reference.

## Binary Size Comparison

| Program | ASM (bytes) | C (bytes) | Delta |
|---------|-------------|-----------|-------|
| arp     | 451         | 454       | +3    |
| cat     | 145         | 143       | -2    |
| chmod   | 149         | 174       | +25   |
| cp      | 268         | 227       | -41   |
| date    | 15          | 15        |  0    |
| dns     | 724         | 1216      | +492  |
| draw    | 245         | 239       | -6    |
| edit    | 1977        | 2348      | +371  |
| hello   | 22          | 23        | +1    |
| ls      | 135         | 172       | +37   |
| mkdir   | 123         | 127       | +4    |
| mv      | 217         | 220       | +3    |
| netinit | 72          | 69        | -3    |
| netrecv | 334         | 386       | +52   |
| netsend | 187         | 221       | +34   |
| ping    | 1019        | 1356      | +337  |
| shell   | 921         | 1326      | +405  |
| uptime  | 50          | 78        | +28   |

**arp (+3):** The three scratch arrays (`mac_buffer[6]`,
`receive_buffer[128]`, `target_ip[4]`) are file-scope BSS globals;
the assembly version uses inline `BUFFER`/`BUFFER+N` offsets.  The
remaining +3 is null terminators on the two `die()` strings.

**chmod (+25):** The assembly version walks the mode argument with
`lodsb` (1 byte per character read); the C version reloads the base
pointer and indexes for each character check.

**dns (+492):** All four buffers (`cname_buffer[128]`, `dns_ip[4]`,
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

**edit (+371):** Both versions implement the same gap-buffer /
kill-buffer editor over the same key bindings.  The C version
translates `ESC [ A/B/C/D` into the matching Ctrl-char before
dispatching, so arrow keys and Ctrl+B/F/N/P share a single move
body — same trick the asm achieves via fall-through to local
labels.  `buffer_character_at` and `column_before` both qualify
for the register calling convention now that `_is_simple_arg`
admits leaf-only `Var ± Int` / `Var ± Var` BinOps; the topological
arg scheduler treats those BinOps as reading whatever pinned
registers their operands map to so an inter-arg dependency
can't trash a live source.  The function still keeps its bp
frame because two of its four params (`buffer`, `gap_end`)
lose pin slots to its body locals, but each call site stops
pushing those two args from registers and just hands `offset`
straight into BX (built in-register via the
`peephole_register_arithmetic` fold of `mov ax, X / add ax, Y /
mov bx, ax`).  Main now also gets a fifth pin slot in BP — the
cost model in `_select_auto_pin_candidates` weighs BP's
zero-clobber-call savings against its 2-byte-per-subscript
penalty (BP can't index DS-relative memory), so it lands on a
high-traffic scalar like `_l_c` while gap_start/gap_end stay
on DI/DX where they cost nothing per subscript.  Cursor
repositioning uses `printf("\e[%d;%dH", ...)` (varargs push /
format scan / `add sp, 6`) where the asm emits a literal ESC
sequence through `FUNCTION_PRINT_CHARACTER`.  char locals spill
to word slots so every byte read comes with a `xor ah, ah`
zero-extension.  The main loop pins its most-used locals
(`gap_start`, `gap_end`, `cursor_line`, `cursor_column`) to
registers and statement-level builtin calls now collapse the
4-register pin save/restore into single-byte `pusha`/`popa`
pairs (vs the per-register push/pop fan the previous codegen
produced); the dispatch chain over `character` hoists a single
`mov ax, [_l_character]` so each `cmp ax, K` is 3 bytes rather
than the 6-byte `cmp word [mem], K` form.  The remaining 10
bytes over the previous 2247-byte build come from the
``_peephole_will_strand_ax`` correctness fix: each of the five
``cursor_line = cursor_line + 1; if (cursor_line >= view_line +
24)`` sites (and their column equivalents) now reloads the
pinned value after the fused ``inc <reg>``, where the old
output elided the reload via an ``ax_local`` shortcut that the
peephole invalidated.  The save-path scratch buffer is now a local
stack array (``char sector[512]``), which triggers a full BP frame
for `main` so the 512-byte array lives on the stack segment rather
than as a data label in the binary (2346 → 2348 with the BP prologue).

**hello (+1):** The C compiler emits a null terminator on every string
literal. The assembly version omits it since `FUNCTION_DIE` uses an
explicit length.

**ls (+37):** The assembly version uses inline `repne scasb` with a
25-byte cap to find the name length, then `FUNCTION_WRITE_STDOUT`
directly; the C version routes through `strlen()` (full 0xFFFF scan
setup) and `write(STDOUT, ...)` (full syscall path via BX=fd).
`entry[DIRECTORY_ENTRY_SIZE]` is a local stack array, triggering a BP
frame for `main` (+6 bytes for prologue, +6 bytes for `lea`-vs-`mov`
addressing on `entry` accesses).

**mkdir (+4):** Null-terminator overhead across 4 string literals.

**netrecv (+52):** The C version uses stack-local `receive_buffer[128]`
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

**ping (+337):** Both versions build ICMP echo requests in userspace
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

**shell (+405):** The archived ``shell.asm`` has been edited so
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

**uptime (+28):** Uses `printf("%02d:%02d:%02d\n", ...)` which pushes
3 args and a format string onto the stack, calls `FUNCTION_PRINTF`,
and cleans up. The assembly version uses inline `FUNCTION_PRINT_DECIMAL`
calls with no stack overhead.
