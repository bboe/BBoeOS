// sb16.c — Sound Blaster 16 (ISA) driver, PCM playback only.
//
// Caller-facing surface (referenced by fs/fd.c, fs/fd/audio.c, and
// the IRQ 5 handler in arch/x86/entry.asm):
//
//     sb16_present (uint8_t) — 1 if the DSP probe matched 0xAA at boot,
//                              0 otherwise.  Read from fd_open's
//                              /dev/audio branch and AUDIO_IOCTL_QUERY.
//     sb16_init () — driver-chain entry; called from entry.asm.
//                    Probes the DSP and, on success, allocates the
//                    4 KB DMA frame permanently (the early-boot
//                    allocation guarantees it lands below the 4 MB
//                    direct-map ceiling; allocating at /dev/audio
//                    open time would risk a frame above that limit
//                    on systems with > 4 MB RAM).  Matches the
//                    network_initialize pattern in drivers/ne2k.c.
//     sb16_open () — per-/dev/audio-open setup: zeros the buffer,
//                    sets sample rate, speaker on, unmasks IRQ 5.
//                    Returns AX = 1, CF clear (the only OOM path —
//                    frame_alloc — already ran at boot).
//     sb16_close () — per-close teardown: speaker off, masks IRQ 5
//                     and 8237 channel 1.  The DMA buffer stays
//                     allocated for the kernel's lifetime; reopen
//                     just re-zeros it.
//     sb16_play (count) — synchronous: programs the 8237 + SB16 DSP
//                         for a single-cycle transfer of `count`
//                         bytes from audio_buffer_kvirt[0..count-1],
//                         blocks via sti+hlt until IRQ 5 fires.
//                         Called from fd_write_audio.
//
// Single-cycle synchronous playback model: each fd_write_audio chunks
// the user buffer into <= 4 KB pieces, copies into audio_buffer_kvirt,
// calls sb16_play to program 8237 ch1 + DSP cmd 0x14 for one chunk,
// blocks via sti+hlt in sb16_play until IRQ 5 fires.  Userland sees
// write() block for the chunk duration — fine for Doom's per-tick
// render pattern (one ~315-sample write per ~28 ms tick).
//
// We could move to auto-init double-buffering for lower latency on
// future audio consumers; not needed for v1 (Doom).
//
// I/O ports inlined as bare integers — same convention as rtc.c /
// fdc.c / ne2k.c (cc.py emits #define as %define which would clash
// with constants.asm's %assigns).  See the SB16_BASE comment block
// in src/include/constants.asm for the canonical port reference table.

uint8_t sb16_present;
asm("sb16_present equ _g_sb16_present");

// audio_buffer_kvirt — 4 KB scratch buffer.  Userland writes here via
// fd_write_audio; sb16_play hands the chunk to the SB16.
uint8_t *audio_buffer_kvirt;
asm("audio_buffer_kvirt equ _g_audio_buffer_kvirt");

// audio_buffer_phys — physical address of the same frame.  Programmed
// into the 8237 channel-1 address + page registers.
uint32_t audio_buffer_phys;
asm("audio_buffer_phys equ _g_audio_buffer_phys");

// IRQ-set flag the blocking writer polls between sti+hlt cycles.
// Cleared by sb16_play before issuing a transfer; set by the IRQ
// handler when DSP signals block-complete.
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

// sb16_close: per-/dev/audio-close teardown.  Single-cycle playback
// is already idle by the time userland calls close (sb16_play returns
// only after IRQ 5 confirms the chunk played), so we just speaker-off
// and mask IRQ 5 + the 8237 channel.  The DMA buffer stays allocated
// for the lifetime of the kernel (allocated permanently in sb16_init);
// reopening just re-zeros it.
void sb16_close() {
    int mask;
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

// sb16_init: probe DSP, then allocate the 4 KB DMA frame.  Inline asm
// wrapper for frame_alloc + DIRECT_MAP_BASE math.  See the file
// header comment for why the alloc lives here rather than sb16_open.
void sb16_init() __attribute__((carry_return));
asm("sb16_init:\n"
    "        call sb16_probe\n"
    "        test eax, eax\n"
    "        jz .sb16_init_no_card\n"
    "        mov byte [_g_sb16_present], 1\n"
    "        call frame_alloc\n"
    "        jc .sb16_init_no_card\n"        // OOM at boot - degrade to "absent"
    "        mov [_g_audio_buffer_phys], eax\n"
    "        add eax, DIRECT_MAP_BASE\n"
    "        mov [_g_audio_buffer_kvirt], eax\n"
    "        ret\n"
    ".sb16_init_no_card:\n"
    "        mov byte [_g_sb16_present], 0\n"
    "        ret\n");

// sb16_open: per-/dev/audio-open setup — zero the DMA buffer (already
// allocated at sb16_init), unmask IRQ 5, set sample rate, speaker on.
// Always succeeds when sb16_present is true.  Returns AX = 1, CF clear.
__attribute__((carry_return))
int sb16_open() {
    int i;
    int mask;
    i = 0;
    while (i < 4096) {
        audio_buffer_kvirt[i] = 128;        // 8-bit unsigned silence midpoint
        i = i + 1;
    }
    audio_wakeup = 0;
    mask = kernel_inb(0x21);
    kernel_outb(0x21, mask & 0xDF);         // unmask IRQ 5 on PIC1
    sb16_dsp_out(0xD1);                     // speaker on
    sb16_dsp_out(0x41);                     // set output sample rate
    sb16_dsp_out(0x2B);                     // 11025 = 0x2B11; high byte first
    sb16_dsp_out(0x11);                     // low byte
    return 1;
}

// sb16_play: program the 8237 + SB16 DSP for a single-cycle transfer
// of `count` bytes from audio_buffer_kvirt[0..count-1], block via
// sti+hlt until the IRQ 5 handler sets audio_wakeup.  count is the
// real byte count, not bytes-1.  Caller is responsible for ensuring
// 0 < count <= 4096.
void sb16_play(int count) {
    int phys;
    int dma_count;
    phys = audio_buffer_phys;
    dma_count = count - 1;
    audio_wakeup = 0;
    kernel_outb(0x0A, 0x05);                            // mask channel 1
    kernel_outb(0x0C, 0);                               // clear flip-flop
    kernel_outb(0x0B, 0x49);                            // single + increment + read + ch 1
    kernel_outb(0x02, phys & 0xFF);                     // address low
    kernel_outb(0x02, (phys >> 8) & 0xFF);              // address high
    kernel_outb(0x83, (phys >> 16) & 0xFF);             // page register for ch 1
    kernel_outb(0x03, dma_count & 0xFF);                // count low
    kernel_outb(0x03, (dma_count >> 8) & 0xFF);         // count high
    kernel_outb(0x0A, 0x01);                            // unmask channel 1
    sb16_dsp_out(0x14);                                 // 8-bit single-cycle PCM output
    sb16_dsp_out(dma_count & 0xFF);
    sb16_dsp_out((dma_count >> 8) & 0xFF);
    while (audio_wakeup == 0) {
        asm("sti\n\thlt");
    }
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
