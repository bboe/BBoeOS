// sb16.c — Sound Blaster 16 (ISA) driver, auto-init double-buffered PCM.
//
// Caller-facing surface (referenced by fs/fd.c, fs/fd/audio.c, and
// the IRQ 5 handler in arch/x86/entry.asm):
//
//     sb16_present (uint8_t) — 1 if the DSP probe matched 0xAA at boot,
//                              0 otherwise.  Read from fd_open's
//                              /dev/audio branch and AUDIO_IOCTL_QUERY.
//     sb16_init () — driver-chain entry; called from entry.asm.
//                    Probes the DSP and, on success, allocates the
//                    4 KB DMA frame and the 4 KB software ring frame
//                    permanently.  Both early-boot allocations land
//                    below the 4 MB direct-map ceiling so the kernel
//                    can reach them at DIRECT_MAP_BASE + phys without
//                    spending a kmap slot.
//     sb16_open () — per-/dev/audio-open setup: zeros the ring + DMA
//                    halves to silence (0x80), resets head/tail/half
//                    state, programs the 8237 in auto-init mode for
//                    the full 4 KB and the DSP cmd 0x1C with count
//                    AUDIO_HALF_SIZE - 1, speaker on, unmasks IRQ 5.
//                    Returns AX = 1, CF clear (the only OOM path —
//                    frame_alloc — already ran at boot).
//     sb16_close () — per-close teardown: DSP 0xD0 (pause) + 0xDA
//                     (exit auto-init), speaker off, masks IRQ 5 and
//                     the 8237 channel.  The DMA + ring frames stay
//                     allocated for the kernel's lifetime; reopen
//                     just re-zeros them.
//     sb16_refill () — IRQ 5 worker (called from pmode_irq5_handler).
//                      Drains AUDIO_HALF_SIZE bytes from the software
//                      ring into the half identified by audio_filling_half,
//                      pads with silence on underrun, flips
//                      audio_filling_half, and sets audio_wakeup so a
//                      ring-full producer parked on sti+hlt advances.
//
// Auto-init double-buffer model: the DSP loops the 8237's 4 KB region
// indefinitely.  IRQ 5 fires every AUDIO_HALF_SIZE bytes (DSP block
// boundary), at which point the just-finished half is free to refill
// while the DSP keeps streaming the other half.  The producer
// (fd_write_audio) writes into the software ring; sb16_refill drains
// it into the just-finished DMA half.  Because writes only need ring
// space, write() returns ahead of playback — Doom's main loop is no
// longer pinned to the SB16's chunk duration the way it was under the
// old single-cycle synchronous model.
//
// I/O ports inlined as bare integers — same convention as rtc.c /
// fdc.c / ne2k.c (cc.py emits #define as %define which would clash
// with constants.asm's %assigns).  See the SB16_BASE comment block
// in src/include/constants.asm for the canonical port reference table.

// Buffer sizes are tuned for low SFX latency.  Doom's i_sound backend
// (tools/doom/i_sound_bboeos.c) renders TICK_SAMPLES = 11025 / 35 ≈ 315
// samples per Doom tick and writes them in one shot; matching
// AUDIO_HALF_SIZE to that exact count means each IRQ 5 boundary is
// also one Doom tick, and a producer that just wrote can't get more
// than one half ahead of the DSP.
//
// Worst-case latency from write() to audible = AUDIO_RING_SIZE (in
// queue) + AUDIO_HALF_SIZE (currently in DMA half being played) =
// 512 + 315 = 827 bytes ≈ 75 ms.  Average is closer to 40 ms because
// the IRQ drains the ring on every half boundary (~28.6 ms).  The
// ring is sized just larger than one tick (512 = next power of two
// above 315) so ring index masking stays a single AND, while still
// absorbing one tick of producer/consumer phase jitter without ever
// silence-padding the half.
#define AUDIO_DMA_SIZE  630     // total DMA buffer (two halves)
#define AUDIO_HALF_SIZE 315     // one DMA half = one Doom tick
#define AUDIO_RING_SIZE 512     // software ring (must be power of two)
#define AUDIO_RING_MASK 511     // = AUDIO_RING_SIZE - 1
#define AUDIO_SILENCE   0x80    // 8-bit unsigned PCM midpoint

uint8_t sb16_present;
asm("sb16_present equ _g_sb16_present");

// audio_buffer_kvirt — 4 KB DMA double-buffer (two AUDIO_HALF_SIZE
// halves).  The 8237 walks it in auto-init mode while the DSP fires
// IRQ 5 at each half boundary; sb16_refill rewrites the just-finished
// half from the software ring.
uint8_t *audio_buffer_kvirt;
asm("audio_buffer_kvirt equ _g_audio_buffer_kvirt");

// audio_buffer_phys — physical address of the DMA frame, programmed
// into the 8237 channel-1 address + page registers at sb16_open.
uint32_t audio_buffer_phys;
asm("audio_buffer_phys equ _g_audio_buffer_phys");

// audio_ring_kvirt — kernel-virt of the 4 KB software ring frame.
// fd_write_audio writes into it (head); sb16_refill drains it (tail).
uint8_t *audio_ring_kvirt;
asm("audio_ring_kvirt equ _g_audio_ring_kvirt");

// Producer / consumer indices into audio_ring_kvirt.  Both are dword-
// aligned globals so their reads and writes are atomic with respect
// to a single-CPU IRQ.  fd_write_audio writes head; sb16_refill writes
// tail.  Empty ring: head == tail.  Full ring: ((head + 1) & MASK) ==
// tail (the canonical "lose one slot to disambiguate" convention).
uint32_t audio_ring_head;
asm("audio_ring_head equ _g_audio_ring_head");

uint32_t audio_ring_tail;
asm("audio_ring_tail equ _g_audio_ring_tail");

// audio_filling_half — 0 or 1; identifies the DMA half that the next
// IRQ 5 will refill.  At sb16_open we prime both halves to silence,
// program the DSP, set audio_filling_half = 0, and start playback —
// so the first IRQ 5 (when half 0 finishes) refills half 0 and flips
// the flag to 1.  Subsequent IRQs alternate.
uint8_t audio_filling_half;
asm("audio_filling_half equ _g_audio_filling_half");

// audio_wakeup — set by sb16_refill on every IRQ 5 so a ring-full
// producer parked on sti+hlt observes the wake and re-checks free
// space.  The producer arms the flag (writes 0) before each hlt;
// any IRQ wakes the CPU but only sb16_refill setting the flag confirms
// the wake came from a buffer drain (not, say, a stray PIT tick — though
// either is fine, the recheck is cheap).
uint8_t audio_wakeup;
asm("audio_wakeup equ _g_audio_wakeup");

// Forward declarations so the function bodies below can sit in
// strict-alphabetical order without per-pair shuffling.  cc.py's
// codegen accepts the same attribute syntax on a forward decl as
// on the definition.
int  sb16_dsp_read();
void sb16_dsp_out(int byte);
void sb16_dsp_wait_write();
int  sb16_probe();
void sb16_reset_delay();

// sb16_close: per-/dev/audio-close teardown.  Pause the DSP, exit
// auto-init mode (0xDA flushes any in-flight block and stops further
// IRQs), speaker off, mask 8237 channel 1 and IRQ 5.  Both the DMA
// buffer and the software ring frame stay allocated for the lifetime
// of the kernel (allocated permanently in sb16_init); reopening
// re-zeros them.
void sb16_close() {
    int mask;
    sb16_dsp_out(0xD0);                     // pause 8-bit DMA
    sb16_dsp_out(0xDA);                     // exit auto-init 8-bit
    sb16_dsp_out(0xD3);                     // speaker off
    kernel_outb(0x0A, 0x05);                // mask 8237 channel 1
    mask = kernel_inb(0x21);                // mask IRQ 5 on PIC1
    kernel_outb(0x21, mask | 0x20);
}

// Send one command/data byte to the DSP.
void sb16_dsp_out(int byte) {
    sb16_dsp_wait_write();
    kernel_outb(0x22C, byte);
}

// Read one byte from the DSP data port, polling DSP_READ_STATUS (bit 7
// = data available).  Returns -1 on timeout (~1 ms).
int sb16_dsp_read() {
    int spins;
    spins = 0;
    while (spins < 1000) {
        if ((kernel_inb(0x22E) & 0x80) != 0) {
            return kernel_inb(0x22A);
        }
        spins = spins + 1;
    }
    return -1;
}

// Wait until the DSP write port can accept a command/data byte.
void sb16_dsp_wait_write() {
    while ((kernel_inb(0x22C) & 0x80) != 0) {
    }
}

// sb16_init: probe DSP, then allocate the 4 KB DMA frame + 4 KB
// software ring frame.  Inline asm wrapper for two frame_alloc calls
// chained through DIRECT_MAP_BASE math.  Either OOM degrades the card
// to "absent" (and frees the DMA frame if the ring alloc was the one
// that failed) so the rest of the kernel's frame budget is unchanged.
void sb16_init() __attribute__((carry_return));
asm("sb16_init:\n"
    "        call sb16_probe\n"
    "        test eax, eax\n"
    "        jz .sb16_init_no_card\n"
    "        call frame_alloc\n"
    "        jc .sb16_init_no_card\n"        // OOM at boot - degrade to "absent"
    "        mov [_g_audio_buffer_phys], eax\n"
    "        add eax, DIRECT_MAP_BASE\n"
    "        mov [_g_audio_buffer_kvirt], eax\n"
    "        call frame_alloc\n"
    "        jc .sb16_init_free_dma\n"
    "        add eax, DIRECT_MAP_BASE\n"
    "        mov [_g_audio_ring_kvirt], eax\n"
    "        mov byte [_g_sb16_present], 1\n"
    "        mov byte [_g_opl3_present], 1\n"
    "        ret\n"
    ".sb16_init_free_dma:\n"
    "        mov eax, [_g_audio_buffer_phys]\n"
    "        call frame_free\n"
    "        mov dword [_g_audio_buffer_phys], 0\n"
    "        mov dword [_g_audio_buffer_kvirt], 0\n"
    ".sb16_init_no_card:\n"
    "        mov byte [_g_sb16_present], 0\n"
    "        mov byte [_g_opl3_present], 0\n"
    "        ret\n");

// sb16_open: per-/dev/audio-open setup.  Zero the ring and both DMA
// halves to silence; reset head/tail/filling_half; program 8237 ch1
// in auto-init mode (0x58) for the full AUDIO_DMA_SIZE; issue DSP
// 0x1C (8-bit auto-init PCM) with count = AUDIO_HALF_SIZE - 1 so the
// DSP fires IRQ 5 at each half boundary.  Speaker on, unmask IRQ 5.
// Always succeeds when sb16_present is true.  Returns AX = 1, CF clear.
__attribute__((carry_return))
int sb16_open() {
    int i;
    int mask;
    int phys;
    int dma_count;
    i = 0;
    while (i < AUDIO_DMA_SIZE) {
        audio_buffer_kvirt[i] = AUDIO_SILENCE;
        i = i + 1;
    }
    i = 0;
    while (i < AUDIO_RING_SIZE) {
        audio_ring_kvirt[i] = AUDIO_SILENCE;
        i = i + 1;
    }
    audio_ring_head = 0;
    audio_ring_tail = 0;
    audio_filling_half = 0;
    audio_wakeup = 0;
    mask = kernel_inb(0x21);
    kernel_outb(0x21, mask & 0xDF);         // unmask IRQ 5 on PIC1
    sb16_dsp_out(0xD1);                     // speaker on
    sb16_dsp_out(0x41);                     // set output sample rate
    sb16_dsp_out(0x2B);                     // 11025 = 0x2B11; high byte first
    sb16_dsp_out(0x11);                     // low byte
    // 8237 mode byte 0x59 = 01 0 1 10 01:
    //   bits 7-6 = 01 single transfer
    //   bit 5    = 0  address increment
    //   bit 4    = 1  auto-init enable (loop the buffer instead of
    //                 stopping at TC; the DSP cmd 0x1C below loops in
    //                 lockstep and fires IRQ at each block boundary)
    //   bits 3-2 = 10 read transfer (memory → peripheral)
    //   bits 1-0 = 01 channel 1
    phys = audio_buffer_phys;
    dma_count = AUDIO_DMA_SIZE - 1;
    kernel_outb(0x0A, 0x05);                            // mask channel 1
    kernel_outb(0x0C, 0);                               // clear flip-flop
    kernel_outb(0x0B, 0x59);                            // single + inc + auto-init + read + ch 1
    kernel_outb(0x02, phys & 0xFF);                     // address low
    kernel_outb(0x02, (phys >> 8) & 0xFF);              // address high
    kernel_outb(0x83, (phys >> 16) & 0xFF);             // page register for ch 1
    kernel_outb(0x03, dma_count & 0xFF);                // count low
    kernel_outb(0x03, (dma_count >> 8) & 0xFF);         // count high
    kernel_outb(0x0A, 0x01);                            // unmask channel 1
    // Classic-DSP auto-init recipe: 0x48 sets the block transfer size
    // (count - 1, so the DSP fires IRQ 5 every AUDIO_HALF_SIZE bytes);
    // 0x1C then starts auto-init 8-bit PCM playback with NO further
    // arguments — it reuses the block size set by the most recent 0x48.
    // Sending count bytes after 0x1C (as 0x14 expects) feeds them to
    // the DSP as fresh commands and silently breaks playback.
    dma_count = AUDIO_HALF_SIZE - 1;
    sb16_dsp_out(0x48);                                 // set block size
    sb16_dsp_out(dma_count & 0xFF);                     // block count low
    sb16_dsp_out((dma_count >> 8) & 0xFF);              // block count high
    sb16_dsp_out(0x1C);                                 // 8-bit auto-init PCM output (no args)
    return 1;
}

// Probe sequence per Creative SB Series Hardware Programming Guide
// section "DSP Reset".  Returns 1 on success (DSP responded with 0xAA),
// 0 otherwise.
int sb16_probe() {
    int response;
    kernel_outb(0x226, 1);
    sb16_reset_delay();
    kernel_outb(0x226, 0);
    response = sb16_dsp_read();
    if (response != 0xAA) {
        return 0;
    }
    return 1;
}

// sb16_refill: IRQ 5 worker.  Called from pmode_irq5_handler in
// entry.asm with interrupts disabled (interrupt gate) — safe to read
// audio_ring_head and update audio_ring_tail without bracketing.
//
// Drains up to AUDIO_HALF_SIZE bytes from the software ring into the
// DMA half indicated by audio_filling_half, pads any remainder with
// silence (underrun → no audible artefact beyond the gap), flips
// audio_filling_half, and sets audio_wakeup so a producer parked on
// sti+hlt re-checks free space.
//
// Worst-case work: 2048-byte memcpy plus a 2048-byte fill loop —
// well under one PIT tick at 1 kHz on QEMU TCG, even before considering
// that this only runs ~5 times/sec (11025 Hz / 2048 samples).
void sb16_refill() {
    int half_offset;
    int filled;
    uint32_t head;
    uint32_t tail;
    if (audio_filling_half == 0) {
        half_offset = 0;
    } else {
        half_offset = AUDIO_HALF_SIZE;
    }
    head = audio_ring_head;
    tail = audio_ring_tail;
    filled = 0;
    while (filled < AUDIO_HALF_SIZE) {
        if (tail == head) {
            break;
        }
        audio_buffer_kvirt[half_offset + filled] = audio_ring_kvirt[tail];
        tail = (tail + 1) & AUDIO_RING_MASK;
        filled = filled + 1;
    }
    audio_ring_tail = tail;
    while (filled < AUDIO_HALF_SIZE) {
        audio_buffer_kvirt[half_offset + filled] = AUDIO_SILENCE;
        filled = filled + 1;
    }
    audio_filling_half = 1 - audio_filling_half;
    audio_wakeup = 1;
}

// Standard SB16 reset delay: four reads of port 0x80 (the unused POST
// diagnostic port).  Each ISA inb is ~1 us; the SB16 DSP needs >= 3 us
// between the reset write of 1 and the write of 0.  Reading 0x80 has
// no architectural side effect on either real PCs or QEMU.
void sb16_reset_delay() {
    kernel_inb(0x80);
    kernel_inb(0x80);
    kernel_inb(0x80);
    kernel_inb(0x80);
}
