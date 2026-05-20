# Archive

Assembly programs that have been rewritten in C. The original assembly source is
kept here for reference.

## Binary Size Comparison

The protected-mode merge made 32-bit the only production target for user
programs (`make_os.sh` passes `--bits 32`); every row in this table is now
32-bit on both sides.

- **ASM 16 (bytes)** — frozen historical 16-bit baseline; never changes once
  recorded.  Useful for spotting how much pmode (operand- size prefixes, dword
  pointer slots, wider PC-relative displacements) inflates each program.
- **ASM (bytes)** — current 32-bit hand-written `archive/<name>.asm` assembled
  size.
- **C (bytes)** — current 32-bit `cc.py` output assembled size.
- **Delta** — C − ASM.  The "where can cc.py do better" signal.

| Program | ASM 16 (bytes) | ASM (bytes) | C (bytes) | Delta |
|---------|----------------|-------------|-----------|-------|
| arp     | 466            | 686         | 587       | -99   |
| cat     | 145            | 185         | 202       | +17   |
| chmod   | 149            | 164         | 221       | +57   |
| cp      | 268            | 328         | 265       | -63  |
| date    | 15             | 21          | 23        | +2    |
| dns     | 724            | 1189        | 1294      | +105  |
| edit    | 2018           | 2659        | 3301      | +642  |
| ls      | 135            | 412         | 697       | +285  |
| mkdir   | 123            | 142         | 163       | +21   |
| mv      | 217            | 242         | 270       | +28   |
| ping    | 1034           | 1230        | 1522      | +292  |
| uptime  | 50             | 67          | 102       | +35   |

**arp (-99):** The three scratch arrays (`mac_buffer[6]`, `receive_buffer[128]`,
`target_ip[4]`) are file-scope BSS globals in both versions now — the asm side
used to reach into the shared `BUFFER` / `BUFFER+N` window at user-virt
`USER_DATA_BASE+0x500`, but the EXEC_ARG handoff removal retired that frame, so
the asm now declares its own `recv_buf times 128 db 0` in the program's data
section.  Two structural wins from 32-bit: the 6-byte MAC copy (was 3×
``movsw``) collapses to ``movsd + movsw``, and the sender-IP match (was two
2-byte ``mov ax, [..]; cmp ax, [..]``) collapses to a single 4-byte compare. The
delta swung negative because the asm now carries the 128-byte receive buffer
inline while the C side still places `receive_buffer` in BSS (counted in the
kernel bss-size trailer, not the .bin size); cc.py pays per-byte ``movzx``
zero-extends on the EtherType / opcode checks but those are now small relative
to the 128-byte buffer the asm carries in its image.

**chmod (+53):** The assembly version walks the mode argument with `lodsb` (1
byte per character read); the C version reloads the base pointer and indexes for
each character check.  The 32-bit asm uses 4-byte argv pointer slots on the user
stack (``[esp+4]`` for argv[1], ``[esp+8]`` for argv[2]); cc.py's 32-bit codegen
for the per-character check sequence carries proportionally more byte-load +
zero-extend overhead than 16-bit, so the delta inflates from +25 to +53.

**dns (+116):** All four buffers (`cname_buffer[128]`, `dns_ip[4]`,
`name_buffer[128]`, `query_buffer[512]`) are file-scope BSS globals; the
assembly version reused `SECTOR_BUFFER` and `BUFFER`, but with `BUFFER` retired
(see the arp note) the asm now declares its own `rr_name_buf` and `cname_buf` as
two 128-byte arrays in the program's data section.  Most of the delta comes from
`decode_domain` and `encode_domain` carrying full stack-frame overhead (push bp
/ mov bp,sp / pop bp / ret per call) and passing arguments via the stack (their
callers pass complex expressions, so the register calling convention doesn't
kick in); `skip_name` is simple enough that cc.py routes its arguments through
registers.  The assembly version uses register calling conventions with no frame
setup for every helper.  The C compiler also generates word-sized loads with
`xor ah,ah` zero-extension for every byte read, whereas the assembly version
uses `lodsb` / `stosb` / `rep movsb` for compact byte-oriented loops.

The 32-bit asm widens dns_query.asm / encode_domain.asm helpers to ESI/EDI plus
dns_base / dns_socket_fd / domain_arg / rr_name_ptr to ``dd 0`` (cc.py 32-bit
stores full EAX through them).  ``mov si, ax`` in decode_domain's
pointer-resolution path becomes ``movzx eax, ax; add eax, [dns_base]; mov esi,
eax`` (the 16-bit form added a 16-bit offset to a 16-bit base; the 32-bit form
needs the zero-extend because [dns_base] is now a 32-bit linear address).  Δ
landed at +116 — cc.py's 32-bit codegen still pays the ``movzx`` zero-extend
cost on every byte read in the answer-walking loop (which the asm avoids), but
the asm version now carries 256 bytes of decode-target buffers inline that the C
version keeps in BSS, so the asm-side image grew enough to outweigh those
per-byte differences.

**edit (+670):** Restored from git history (retired during the pmode merge
because the 16-bit C build couldn't represent a 256 KB buffer base).  The 32-bit
asm rewrites the gap buffer to live at ``EDIT_BUFFER_BASE = 0x100000`` (1 MB
mark, past the VGA / BIOS regions) with a 1 MB ``EDIT_BUFFER_SIZE`` and
``EDIT_KILL_BUFFER`` at the 2 MB mark — same layout the C version uses.  The
16-bit baseline of 2018 bytes used a buffer floating on ``program_end`` inside
segment 0 with a 25 KB cap.

The +670 delta is mostly two structural costs cc.py 32-bit pays that the asm
avoids: (1) every byte read from the gap buffer pays ``movzx ecx, byte [..]``
zero-extend instead of asm's bare ``mov al, [..]``; (2) cc.py spills the
``cursor_column`` / ``cursor_line`` / ``view_*`` BSS globals through EAX for
every increment / compare where the asm hits memory directly.  The deep call
graph (``move_*``, ``buf_*``, ``check_*``, ``do_*`` helpers) also pays for
register spilling cc.py's IR-based codegen does at every helper boundary.

**ls (+19):** The assembly version uses inline `repne scasb` with a 25-byte cap
to find the name length, then `FUNCTION_WRITE_STDOUT` directly; the C version
routes through `strlen()` and `write(STDOUT, ...)`.
`entry[DIRECTORY_ENTRY_SIZE]` is a local stack array, triggering a BP frame for
`main`.  Δ shrunk from +30 to +19 in 32-bit because the asm grew faster (+44 vs
C's +31) — both the ``repne scasb`` walk-and-subtract and the ``test byte
[entry_buf+OFFSET]`` flag checks gain operand-size overhead in the hand-written
form, which narrows the compiler-vs-handwritten gap. The +2 shift over the
original +17 comes from cc.py preserving ``cld`` ahead of the C ``strlen``'s
``repne scasb``; the prefix used to be stripped by ``peephole_unused_cld``,
leaving DF undefined in practice (typically clear via boot defaults).

**mkdir (+20):** The asm version stores fd / pointer / count in registers; cc.py
32-bit spills several into BP-relative locals (visible in the prologue's wider
``sub esp, N``), and string- literal handling adds a null terminator per string
× 4 strings. The 16-bit baseline's +4 was almost entirely null-terminator
overhead; the +20 here picks up cc.py's frame setup and 32-bit prologue/epilogue
cost too.

**ping (+327):** Both versions build ICMP echo requests in userspace over the
same ``SYS_NET_OPEN (SOCK_DGRAM, IPPROTO_ICMP)`` / ``SYS_NET_SENDTO`` /
``SYS_NET_RECVFROM`` path.  The four scratch arrays (``dns_ip[4]``,
``packet_buffer[128]``, ``query_buffer[512]``, ``target_ip[4]``) are local stack
arrays in `main`; ``query_buffer`` and ``dns_ip`` are passed as parameters to
``resolve_dns`` so the helper no longer reads file-scope globals.  Most of the
delta is the DNS fallback: ``encode_domain`` and ``resolve_dns`` carry full
stack-frame overhead (push bp / mov bp,sp / pop bp / ret) and pass arguments via
the stack — their call sites include complex expressions so cc.py can't switch
them to register passing. ``skip_name`` is simple enough that its two call sites
now hand arguments in registers.  The asm version inlines the equivalent logic
for every helper using register calling conventions. Fixed-byte header layouts
(DNS query header, QTYPE/QCLASS tail, ICMP echo template) use ``memcpy`` from
short string-literal constants instead of per-byte assignments, which collapses
each ~8 × ``mov byte [...], imm`` burst into a single ``rep movsb``.

**uptime (+33):** Uses `printf("%02d:%02d:%02d\n", ...)` which pushes 3 args and
a format string onto the stack, calls `FUNCTION_PRINTF`, and cleans up. The
assembly version uses inline `FUNCTION_PRINT_DECIMAL` calls with no stack
overhead.  The 32-bit asm rewrite drops the ``mov cl, 60 / div cl`` byte-divide
trick (which left AH=remainder) in favour of two uniform ``xor edx, edx / div
ecx`` 32-bit divides with EDX as remainder — slightly bigger but consistent.
