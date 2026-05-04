// fd/audio.c — read/write/ioctl/close handlers for FD_TYPE_AUDIO
// (/dev/audio).  Companion to drivers/sb16.c.
//
// Single-cycle synchronous playback model: each fd_write_audio splits
// the user buffer into <= 4 KB chunks, copies each chunk into the
// driver's audio_buffer_kvirt, and calls sb16_play which programs
// the 8237 + SB16 DSP for a single-cycle transfer and blocks via
// sti+hlt until IRQ 5 fires (signalling block-complete).  Returns
// the full count once all bytes have played.
//
// Synchronous rather than auto-init double-buffering for v1: simpler
// to reason about and the latency floor (chunk duration) matches
// Doom's per-tick write pattern (~315 samples per ~28 ms tick).  An
// auto-init double-buffered model would be lower latency but isn't
// needed today — drivers/sb16.c's sb16_play encapsulates the write
// model cleanly so a future change is local.

extern uint8_t sb16_present;
extern uint8_t *audio_buffer_kvirt;
extern uint8_t *fd_write_buffer;

// Forward decls into drivers/sb16.c.
void sb16_play(int count);

#define AUDIO_CHUNK_MAX 4096

// fd_close_audio: per-AUDIO close hook called from fd_close.  Real
// teardown (DSP speaker-off, mask DMA + IRQ) lives in sb16_close in
// drivers/sb16.c, which fd_close invokes directly; the per-fd hook
// here is reserved for any future per-fd state that needs unwinding
// before the slot is zeroed.
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
// SB16 scratch buffer (in chunks of <= AUDIO_CHUNK_MAX) and synchronously
// play each chunk via sb16_play.  Always returns AX = count, CF clear.
__attribute__((carry_return))
int fd_write_audio(int *bytes_written __attribute__((out_register("ax"))),
                   int count __attribute__((in_register("ecx")))) {
    int written;
    int chunk;
    int index;
    written = 0;
    while (written < count) {
        chunk = count - written;
        if (chunk > AUDIO_CHUNK_MAX) {
            chunk = AUDIO_CHUNK_MAX;
        }
        index = 0;
        while (index < chunk) {
            audio_buffer_kvirt[index] = fd_write_buffer[written + index];
            index = index + 1;
        }
        sb16_play(chunk);
        written = written + chunk;
    }
    *bytes_written = count;
    return 1;
}
