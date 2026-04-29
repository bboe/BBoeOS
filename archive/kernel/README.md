# Kernel Archive

Hand-written assembly versions of kernel files that have since been
rewritten in C, kept here for reference and size tracking.

This is the kernel-side analogue of [`archive/README.md`](../README.md),
which tracks user-space programs.  The split is intentional: kernel
files don't assemble standalone (they `%include` each other and reach
into shared globals), so the measurement mechanism differs from the
per-program builds the user-space archive uses.

## Binary Size Comparison

Each row records the assembled byte size of one kernel file, measured
two ways:

- **ASM (bytes)**: `os.bin` size when this one file is swapped to its
  archived hand-written form (everything else stays in C).
- **C (bytes)**: `os.bin` size when the entire kernel is in its
  current C form.  Same value across rows because the C-side build
  is the snapshot we publish.
- **Delta**: `C − ASM`.  Positive deltas are where cc.py emits more
  bytes than the hand-written original — those are the candidates
  for compiler optimization work.

Refresh with `tools/measure_kernel_ports.sh`, which runs the full
swap-and-rebuild loop and prints rows in this format directly.

The path key (left column) matches the file's location under `src/`,
minus the `.asm` extension (e.g. `drivers/ps2` is `src/drivers/ps2.asm`
archived as `archive/kernel/drivers/ps2.asm`, ported to
`src/drivers/ps2.c`).

| File | ASM (bytes) | C (bytes) | Delta |
|------|-------------|-----------|-------|
| arch/x86/system | 39310 | 39334 | +24 |
| drivers/ata | 39062 | 39334 | +272 |
| drivers/console | 38846 | 39334 | +488 |
| drivers/fdc | 39086 | 39334 | +248 |
| drivers/ne2k | 38942 | 39334 | +392 |
| drivers/ps2 | 38934 | 39334 | +400 |
| drivers/rtc | 38958 | 39334 | +376 |
| drivers/serial | 39310 | 39334 | +24 |
| drivers/vga | 39014 | 39334 | +320 |
| fs/fd/console | 39286 | 39334 | +48 |
| fs/fd/fs | 39150 | 39334 | +184 |
| fs/fd/net | 39326 | 39334 | +8 |

## Annotations

Per-file notes explaining each delta land in this section as ports
arrive.  The pattern: open with the file path + signed delta, follow
with the structural reason cc.py's output differs from the
hand-written asm.  Useful for spotting cc.py optimization
opportunities — e.g. "spilling X to a stack slot the asm kept in BX",
"materializing a `&array[i]` address that constant-folded in asm",
etc.

**drivers/ps2 (+408):** cc.py's IR-based codegen materialises every
intermediate to a local stack slot and then loads it back.  The asm
version of `ps2_handle_scancode` reads the scancode once into AL and
keeps it in EAX through the whole modifier-key dispatch chain; the C
port spills the parameter to `[ebp-N]` at the prologue and reloads
it for each `cmp` against the modifier scancode constants.  Same
shape for the local `code` / `ascii` / `upper` variables in the
regular-key path — each gets its own slot.  `ps2_putc` shows the
same pattern: the asm version uses `[ps2_tail]` directly with byte
arithmetic on `ECX/EDX`; the C version stores `tail` to a local,
computes `next` to another, branches on `[ebp-next]`, etc.  The 32-
bit prologue / epilogue overhead (`push ebp; mov ebp, esp; sub esp,
N` then `mov esp, ebp; pop ebp`) costs a fixed ~7 bytes per function
× 5 functions = ~35 bytes more than the asm version's bare `push
eax; ...; pop eax; ret` shape.  Candidate cc.py optimizations: keep
single-use locals in registers across straight-line code (avoid the
spill/reload roundtrip), and recognise the "param read once, used
many cmps" pattern where the spill is unnecessary.

**drivers/serial (+32):** `serial_character` is the only function — a
ten-line poll-then-write pair against COM1.  Overhead is dominated by
cc.py's frame setup: the `__attribute__((preserve_register("ax")))` /
`("dx")` envelope adds a fixed `push ax; push dx; ...; pop dx; pop ax`
around the body; the `kernel_inb` / `kernel_outb` calls each emit
`mov dx, <port>; in al, dx` / `mov dx, <port>; mov al, <byte>; out
dx, al` that the hand-written asm could reuse `dx` across.  Dead-code
removal of `serial_getc` (no callers; `fs/fd/console.asm` polls COM1
inline) trimmed both sides equally and kept the delta small.

**arch/x86/system (+24):** Two tiny functions, one with a never-returns
infinite loop.  `shutdown` is byte-for-byte equivalent to the asm
version (`mov dx, port; mov ax, 0x2000; out dx, ax` × 2 + `ret`).
`reboot` pays the cc.py 12-byte prologue (`push ebp; mov ebp, esp;
sub esp, 8`) it will never return through, plus a 3-byte epilogue
that's also unreachable — together they're the entire +27.  Could
be reclaimed with `__attribute__((naked))` if cc.py grows support
for it on functions whose body is "linear C with `asm()` escapes".

**drivers/console (+480):** The ANSI escape parser becomes ~110 lines
of pure C against five `vga_*` helpers (`vga_teletype`,
`vga_get_cursor`, `vga_set_cursor`, `vga_set_bg`,
`vga_write_attribute`) and `serial_character`.  Cross-asm calls use
`__attribute__((in_register))` for the AL/BX/DX inputs and one
`out_register` parameter to capture `vga_get_cursor`'s packed DX
return.  `put_character`'s `preserve_register("ax/bx/cx/dx")`
matches the asm contract (`push eax/ebx/ecx/edx` ... `pop ...`).
Most of the +480 is cc.py's pattern of spilling every intermediate
to a stack slot — `p1`, `dx_packed`, `row`, `col`, `linear` each
get their own `[ebp-N]`, then reload through EAX.  The asm version
keeps these in BX/CX/DX across the dispatch chain.  `ansi_params`
shifts from ``dw 0,0,0`` (6 bytes) to four-byte int slots (12
bytes) — small but cumulative — and the four `vga_get_cursor`
call sites each pay the full out-register-capture sequence
(`mov [ebp-N], dx`).  Candidate cc.py optimizations: register
pinning across straight-line code in fastcall-ish paths, and a
narrower default storage for `int`-typed globals when their value
range fits a smaller width.

**drivers/ata (+272):** Five functions (`ata_init`, `ata_issue`,
`ata_wait_drq`, `ata_read_sector`, `ata_write_sector`) all riding
on `kernel_inb` / `kernel_outb` plus `kernel_insw` / `kernel_outsw`
for the 256-word PIO data transfer.  `carry_return` carries
the asm CF=err contract intact (`return 1` → CF clear / success;
`return 0` → CF set / error — the inverted mapping from cc.py's
convention).  The +264 is mostly the same per-function frame
overhead pattern as serial: cc.py's `push ebp; sub esp, N; ...
mov esp, ebp; pop ebp` envelope on every helper, plus the
`preserve_register("eXx")` pushes/pops on each E-reg the asm
version preserved (saving full 32-bit regs is critical here —
bbfs holds full 32-bit ECX file sizes that 16-bit `push cx`
would silently truncate).  The wider `mov edx, [ebp-N]; and edx,
65535` reload-and-mask on `lba` is also paid once per call
where the asm version stayed in CX through the port writes.

**drivers/rtc (+368):** Eleven functions; the C-shaped logic
(`rtc_bcd_to_bin`, `rtc_is_leap_year`, the year-loop and
month-days arithmetic in `rtc_read_epoch_impl`) ports to ~70
lines of straightforward C.  The non-C-shaped pieces stay in
file-scope `asm()` blocks: `rtc_read_date_internal` /
`rtc_read_time_internal` (multi-byte CH:CL/DH:DL returns that
don't fit cc.py's single-EAX return shape — the C side picks
them up via `out_register("cx")` / `out_register("dx")`
parameters), `rtc_tick_read` (cli/popf-bracketed atomic 32-bit
read of `system_ticks`), `uptime_seconds` (the EAX = ticks /
TICKS_PER_SECOND division), `rtc_sleep_ms` (16-bit CX
input, pushf/sti envelope, full register preservation), and
`rtc_read_epoch` (a thin shim that calls
`rtc_read_epoch_impl` and splits the 32-bit EAX into DX:AX
for asm-side callers).  +368 is mostly the cc.py prologue /
epilogue overhead on the substantial helper chain
(`rtc_read_epoch_impl` calls `rtc_read_date_internal`,
`rtc_read_time_internal`, `rtc_bcd_to_bin` × 7,
`rtc_is_leap_year` × N) plus stack spills for every
intermediate (`year`, `days`, `month_index`, `seconds`,
`cx`, `dx`).  The asm version kept these in BX/CX/SI through
the year-loop.

**drivers/fdc (+224):** Thirteen functions covering DMA + IRQ-driven
floppy I/O.  The substantive C content is the public surface
(`fdc_init`, `fdc_drain_result`, `fdc_sense_interrupt`, `fdc_dma_setup`)
plus the read/write entry points (`fdc_read_sector`, `fdc_write_sector`)
which orchestrate calls into the asm-shaped helpers.  The helpers
themselves stay in file-scope `asm()` blocks because they don't fit
cc.py's natural codegen shape: `fdc_send` / `fdc_recv` (tight
poll-then-port-IO, preserve AX/DX), `fdc_wait_irq` (pushf/sti
envelope plus a tight flag spin), `fdc_lba_to_chs_internal`
(multi-byte CH:CL/DH:DL return via two `out_register` parameters),
`fdc_motor_start` / `fdc_seek` / `fdc_issue_read_write` (multi-step
asm-state-machine sequences with all-register preservation), the
IRQ 6 stub (`iretd`), and `fdc_install_irq` (address-of-label).
+240 is the per-function frame overhead on the C wrappers
(`fdc_drain_result`'s while loop, `fdc_sense_interrupt`'s three
calls, `fdc_dma_setup`'s ten `kernel_outb`s, `fdc_init`'s reset +
sense × 4 + SPECIFY × 3, the read/write `cx`/`dx` spill+reload
pattern around the `out_register` capture from
`fdc_lba_to_chs_internal`).

**drivers/ne2k (+392):** Five public functions covering NE2000 polled-mode
Ethernet.  `ne2k_probe`, `ne2k_init`, `ne2k_send`, and
`network_initialize` port to straight-line C — `kernel_outb` for
register programming, `kernel_inw` for the word-mode PROM read inside
`ne2k_probe`, and `kernel_outsw` for the TX-buffer DMA upload in
`ne2k_send` (matches the asm `rep outsw`).  `ne2k_send`'s
``__attribute__((carry_return))`` keeps the asm CF=err contract under
the inverted `return 1` = success mapping; `in_register("esi")` /
`in_register("ecx")` pin the frame pointer and length the asm callers
in `net/ip.c`, `net/arp.asm`, and `fs/fd/net.asm` pass via those
registers.  `ne2k_receive` stays in a file-scope `asm()` block — its
multi-register return contract (EDI = NET_RECEIVE_BUFFER, ECX = packet
length, CF = packet-available flag) doesn't fit cc.py's single-EAX
return shape, and the four asm callers (`net/arp.asm`,
`net/icmp.c`'s asm body, `net/udp.asm`, `fs/fd/net.asm`) all read
back EDI immediately to walk the received frame.  `mac_address[6]`
and `net_present` are file-scope globals with `equ _g_*` aliases so
asm consumers under the bare names continue to resolve.  Most of the
+400 is the same per-function frame overhead pattern other drivers
hit (`push ebp; mov ebp, esp; sub esp, N` × 4 functions plus the
`preserve_register` push/pops on `ne2k_send`); `ne2k_probe`'s two
`while` loops over `mac_address[i]` add ~20 bytes vs the asm
`mov esi, mac_address; lodsb; out dx, al; loop` shape.

**drivers/vga (+320):** Eleven functions covering text-mode framebuffer,
mode-13h pixel I/O, the CRTC cursor pair, mode programming, and the
`/dev/vga` ioctl backend.  The C-shaped pieces are the register-
banging helpers (`vga_get_cursor`, `vga_set_cursor`, `vga_set_bg`,
`vga_set_palette_color`) and `vga_fill_block`'s 8x8 mode-13h tile
fill (a nested while loop over `far_write8` calls — the asm
version's `rep stosb` per row is more compact, but per-byte stores
fit cc.py's natural codegen and stay self-contained without an
asm() escape).  The framebuffer-touching functions stay in
file-scope `asm()` blocks: `vga_clear_screen` and `vga_scroll_up`
(`rep stosw` over 2000 cells / `rep movsw` over 1920 cells beat
per-cell `far_write16` loops both for runtime and emitted bytes),
`vga_teletype` and `vga_write_attribute` (cursor-fetch + linear-
offset arithmetic + cell write are short enough that cc.py's
prologue/preserve_register overhead would dominate the function),
and `vga_set_mode` (the `lodsb`-driven traversal of `vga_mode_table`
is the natural shape).  `fd_ioctl_vga` stays in asm because it's a
syscall-jmp target with a register-state contract cc.py's
prologue/epilogue would clobber.  Trailing data tables
(`vga_current_mode`, `vga_default_palette[48]`, `vga_mode_table[122]`)
become cc.py global byte arrays; `vga_mode_table_end` is an `equ`
against `vga_mode_table + 122` so the asm() loop's bound check
still folds.  +328 is mostly cc.py's per-function frame overhead
(push ebp / sub esp / preserve_register pushes) on the five pure-C
helpers, plus the C-shaped `vga_get_cursor`'s divmod-by-80 (~+96
vs the asm version's `div bx`), plus the new `movzx eax, ax`
prologue prefix on every narrow-pinned in_register parameter (PR
#241 fix).

**fs/fd/net (+16):** Two tiny functions — `fd_read_net` (poll NIC,
memcpy frame) and `fd_write_net` (send raw frame from
`fd_write_buffer`) — port to ~30 lines of straight-line C.  Tightening
`ne2k_receive`'s C declaration with `out_register("edi")` /
`out_register("ecx")` + `carry_return` lets the C side capture the
multi-register output cleanly (no asm shim needed; the asm body of
`ne2k_receive` is unchanged).  `fd_write_buffer` lifts out of
`fs/fd.c`'s asm() block to a C-level `uint8_t *` global with an
`asm("fd_write_buffer equ _g_fd_write_buffer")` shim so the surviving
`fs/fd/{console,fs}.asm` references resolve unchanged.  The +16 is
mostly cc.py's per-function frame setup on the two ports plus the
prologue zero-extend around the `in_register("ecx")` count parameter.

**fs/fd/console (+56):** Two functions — `fd_read_console` (poll
keyboard ring + COM1, CR→LF translate, write one byte to user
buffer) and `fd_write_console` (loop `put_character` over the user
buffer at `[fd_write_buffer]`).  +56 is mostly cc.py's frame setup
on the read path: the polling loop's two `kernel_inb` call sites
(LSR data-ready check, then DR read) each emit `mov dx, <port>; in
al, dx; xor ah, ah` whereas the asm version reused DX across the
adjacent reads.  The `asm("sti")` escape costs one byte.
`destination[0] = byte` (rather than `*destination = byte` — cc.py
rejects the latter on non-`out_register` parameter pointers) emits
the same `mov [edi], al` either way.
