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
| drivers/ps2 | 37102 | 37510 | +408 |

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
