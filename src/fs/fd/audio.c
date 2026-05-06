// fd/audio.c — read/write/ioctl/close handlers for FD_TYPE_AUDIO
// (/dev/audio).  Companion to drivers/sb16.c.
//
// Auto-init double-buffer model: drivers/sb16.c programs the SB16 in
// auto-init mode at sb16_open, so the DSP loops the AUDIO_DMA_SIZE DMA
// buffer (two AUDIO_HALF_SIZE halves) indefinitely and IRQ 5 fires at
// each half boundary.  The IRQ 5 handler (pmode_irq5_handler in
// entry.asm) calls sb16_refill, which drains the software ring
// (audio_ring_kvirt) into the just-finished half and pads with silence
// on underrun.
//
// fd_write_audio is the ring producer: it copies user bytes from
// fd_write_buffer into audio_ring_kvirt, blocks on sti+hlt only when
// the ring fills, and returns once every byte is queued.  AUDIO_RING_SIZE
// (512 bytes ≈ 46 ms at 11025 Hz) is sized just above Doom's per-tick
// burst (TICK_SAMPLES = 315) so write() never blocks for typical Doom
// usage but the ring still bounds total queue depth — total worst-case
// SFX latency stays at AUDIO_RING_SIZE + AUDIO_HALF_SIZE ≈ 75 ms.  Keep
// these in sync with the matching macros in drivers/sb16.c.
extern uint8_t sb16_present;
extern uint8_t *audio_ring_kvirt;
extern uint32_t audio_ring_head;
extern uint32_t audio_ring_tail;
extern uint8_t audio_wakeup;
extern uint8_t *fd_write_buffer;

#define AUDIO_RING_SIZE 512
#define AUDIO_RING_MASK 511

// fd_close_audio: per-AUDIO close hook called from fd_close.  Real
// teardown (DSP pause + exit auto-init, speaker off, mask DMA + IRQ)
// lives in sb16_close in drivers/sb16.c, which fd_close invokes
// directly; the per-fd hook here is reserved for any future per-fd
// state that needs unwinding before the slot is zeroed.
void fd_close_audio() {
}

// fd_ioctl_audio: SYS_IO_IOCTL backend for /dev/audio fds.  Called via
// `jmp` from fd_ioctl with AL = cmd, ESI = fd entry pointer.  Inline
// asm because the syscall jump-table dispatch enters with a register-
// state contract cc.py's prologue/epilogue would clobber.
//
//   AUDIO_IOCTL_QUERY (0) — AX = sb16_present (0 or 1), CF clear.
//   anything else         — CF set.
void fd_ioctl_audio();

asm("fd_ioctl_audio:\n"
    "        cmp al, 0x00\n"                        // AUDIO_IOCTL_QUERY
    "        jne .fd_ioctl_audio_bad\n"
    "        movzx eax, byte [_g_sb16_present]\n"   // zero-extend so high AX is clean
    "        clc\n"
    "        ret\n"
    ".fd_ioctl_audio_bad:\n"
    "        stc\n"
    "        ret\n");

// fd_write_audio: copy `count` bytes from fd_write_buffer into the
// software ring.  Block on sti+hlt only when the ring is full;
// otherwise return immediately once everything is queued.  Always
// returns AX = count, CF clear.
//
// Race contract: only this function modifies audio_ring_head; only
// the IRQ 5 path (sb16_refill) modifies audio_ring_tail.  Both indices
// are dword-aligned so single-instruction reads are atomic on x86 vs.
// a single-CPU IRQ.  The producer's snapshot of `tail` may be stale
// if IRQ 5 advances it between the read and the head update — that's
// safe, since `tail` only ever increases (more free space, never less),
// so the snapshot is always a conservative lower bound on free space.
__attribute__((carry_return))
int fd_write_audio(int *bytes_written __attribute__((out_register("ax"))),
                   int count __attribute__((in_register("ecx")))) {
    int written;
    uint32_t head;
    uint32_t tail;
    uint32_t free_bytes;
    int chunk;
    int index;
    written = 0;
    while (written < count) {
        head = audio_ring_head;
        tail = audio_ring_tail;
        // Free slots = (RING_SIZE - 1) - used; the -1 reserves one
        // slot to disambiguate empty (head == tail) from full.
        free_bytes = (AUDIO_RING_SIZE - 1) - ((head - tail) & AUDIO_RING_MASK);
        if (free_bytes == 0) {
            // Arm the wakeup flag and park; sb16_refill sets it on
            // every IRQ 5, so the next half boundary releases us.
            // Any stray IRQ wakes the hlt — we just re-check and
            // re-park if needed.
            audio_wakeup = 0;
            asm("sti\n\thlt");
            continue;
        }
        chunk = count - written;
        if ((uint32_t)chunk > free_bytes) {
            chunk = free_bytes;
        }
        index = 0;
        while (index < chunk) {
            audio_ring_kvirt[(head + index) & AUDIO_RING_MASK] = fd_write_buffer[written + index];
            index = index + 1;
        }
        // Single dword store — atomic on x86, so the IRQ either sees
        // the old or new head, never a partial update.
        audio_ring_head = (head + chunk) & AUDIO_RING_MASK;
        written = written + chunk;
    }
    *bytes_written = count;
    return 1;
}
