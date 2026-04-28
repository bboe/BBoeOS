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
| arch/x86/system | 37486 | 37510 | +24 |
| drivers/console | 37030 | 37510 | +480 |
| drivers/ps2 | 37102 | 37510 | +408 |
| drivers/serial | 37478 | 37510 | +32 |

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
