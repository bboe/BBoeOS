/* tools/doom/opl_bboeos.c — Chocolate Doom OPL backend that pushes
 * register writes to /dev/midi as 6-byte commands.  Implements the
 * API declared in third_party/chocolate-doom-opl/opl.h.
 *
 * Single-threaded, poll-driven design.  There is no audio thread or
 * signal handler in BBoeOS userland, so timing has to be advanced
 * deliberately from the same context that consumes it.
 * doomgeneric_Tick (one frame) calls S_UpdateSounds → I_UpdateSound
 * → music_module->Poll → BBoe_MusicPoll → opl_bboeos_poll, which:
 *
 *   1. Sets music_clock_us = (DG_GetTicksMs() - clock_anchor_ms) * 1000
 *      unless the song is paused.  music_clock_us therefore tracks
 *      wall-clock time, not the frame rate — when Doom's per-frame
 *      work pushes the loop below 35 fps, music keeps the right
 *      tempo instead of slowing in lockstep with the rendering.
 *   2. Fires every callback whose fire_at_us has elapsed.  Each
 *      callback is the engine's TrackTimerCallback / RestartSong;
 *      they call OPL_WriteRegister to enqueue 6-byte commands.
 *   3. Flushes the coalescing buffer to /dev/midi in one write().
 *
 * Per-command timing precision below 1 ms comes from the kernel
 * side: each command carries a `delay` field (16-bit, milliseconds
 * since the previous command in this fd's stream); the kernel ISR
 * runs at 1 kHz and walks the queue tick-by-tick.  Userland just
 * needs to hand it the right delay deltas, which we derive from
 * music_clock_us / 1000.
 *
 * The OPL_Lock / OPL_Unlock entry points are no-ops because there's
 * nothing to lock against.  Functions that the upstream
 * SDL/Allegro backends use but i_oplmusic.c does not call
 * (OPL_WritePort, OPL_ReadPort, OPL_ReadStatus, OPL_Detect,
 * OPL_Delay) are stubs that return sensible defaults so the linker
 * stays satisfied if some other vendored module ever pulls them in.
 *
 * Function definitions are in alphabetical order by visible name
 * (helpers first, then OPL_ then opl_bboeos_), per CLAUDE.md. */

#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#include "opl.h"
#include "doomgeneric.h"     /* DG_GetTicksMs — wall-clock anchor for music_clock_us */

/* /dev/midi wire-format constants (mirror src/include/constants.asm
 * MIDI_IOCTL_QUERY / MIDI_IOCTL_FLUSH).  Stored locally so this file
 * doesn't need to reach into kernel includes.  FREEZE_GAP_MS guards
 * against long stalls in the main loop (see clock_anchor_ms below). */
#define COALESCE_BYTES    (COALESCE_SLOTS * COMMAND_BYTES)
#define COALESCE_SLOTS    64
#define COMMAND_BYTES     6
#define FREEZE_GAP_MS     100
#define MAX_CALLBACKS     32
#define MIDI_IOCTL_FLUSH  0x01

struct callback_entry {
    opl_callback_t callback;
    void          *data;
    uint64_t       fire_at_us;
    int            in_use;
};

static int      anchor_set = 0;
static struct callback_entry callbacks[MAX_CALLBACKS];
/* clock_anchor_ms is the DG_GetTicksMs() reading that corresponds to
 * music_clock_us == 0.  Set on the first opl_bboeos_poll after init,
 * and re-anchored on resume so paused wall-clock time isn't billed to
 * the music.  anchor_set guards the first-poll initialisation so we
 * don't bake the boot offset into the first emitted command's delay.
 *
 * last_poll_ms tracks the most recent DG_GetTicksMs() reading so the
 * poll can detect long gaps (level loads, end-of-level tally screens,
 * blocking WAD I/O — anything that stalls the main loop for more
 * than FREEZE_GAP_MS).  On a long gap we slide clock_anchor_ms
 * forward by the entire inter-poll interval so music_clock_us stays
 * put.  Without that slide, the post-gap poll's due-callback loop
 * fires every callback the engine queued for the missed seconds at
 * once: the 64-event coalesce buffer and 256-event kernel ring both
 * overflow, flush_coalesce drops bytes on the short write, and the
 * OPL chip is left in a stale half-applied state — audible as a
 * stuck-voice hang for a beat or two after the freeze ends. */
static uint32_t clock_anchor_ms = 0;
static uint8_t  coalesce_buffer[COALESCE_BYTES];
static int      coalesce_length = 0;
/* last_emitted_ms is the music-clock value (in milliseconds, the
 * unit /dev/midi uses for its `delay` field) at the moment of the
 * most recently emitted command.  Each new command's delay is
 * (music_clock_us / 1000) - last_emitted_ms. */
static uint32_t last_emitted_ms = 0;
static uint32_t last_poll_ms = 0;
static int      midi_fd = -1;
static uint64_t music_clock_us = 0;
static int      paused = 0;

/* Append one 6-byte command into the coalescing buffer; flush
 * first if it would overflow.  Bytes are little-endian per the
 * struct in the design doc:
 *   uint16_t delay;  uint8_t bank;  uint8_t reg;
 *   uint8_t  value;  uint8_t reserved (= 0). */
static void append_command(uint16_t delay, uint8_t bank, uint8_t reg, uint8_t value);

/* Find the callback slot with the smallest fire_at_us <= now_us,
 * or -1 if none are due.  Linear scan over MAX_CALLBACKS — fine at
 * 35 Hz with ≤ 32 active callbacks; Doom rarely has more than one
 * per active MIDI track (typically 1-4). */
static int find_next_due_callback(uint64_t now_us);

/* Flush the coalescing buffer to /dev/midi, retrying on short
 * writes (the kernel ring drains at 1 kHz so a queued-up burst can
 * temporarily exceed ring capacity).  No-op when fd is closed or
 * the buffer is empty. */
static void flush_coalesce(void);

void OPL_AdjustCallbacks(float factor) {
    /* Match chocolate-doom's OPL_Queue_AdjustCallbacks
     * (third_party/chocolate-doom-opl/opl_queue.c:204): scale the remaining
     * time by 1/factor, not factor.  MetaSetTempo passes
     * `factor = us_per_beat_old / tempo_new`, so a tempo *decrease*
     * (tempo_new > us_per_beat_old) yields factor < 1 and must
     * stretch the offset (offset / factor > offset) so the next
     * beat fires later.  The previous `* factor` form did the
     * inverse and accelerated the song on every tempo slowdown. */
    int i;
    uint64_t remaining;
    for (i = 0; i < MAX_CALLBACKS; i = i + 1) {
        if (!callbacks[i].in_use) {
            continue;
        }
        if (callbacks[i].fire_at_us <= music_clock_us) {
            /* Already due; leave it alone — it'll fire on the next
             * find_next_due_callback walk. */
            continue;
        }
        remaining = callbacks[i].fire_at_us - music_clock_us;
        remaining = (uint64_t)((float)remaining / factor);
        callbacks[i].fire_at_us = music_clock_us + remaining;
    }
}

void OPL_ClearCallbacks(void) {
    int i;
    for (i = 0; i < MAX_CALLBACKS; i = i + 1) {
        callbacks[i].in_use = 0;
    }
    /* Silence the chip *now*, not at the next opl_bboeos_poll.
     * I_OPL_StopSong calls this right before AllNotesOff queues
     * KEY_OFF events into the userland coalesce buffer; without an
     * explicit chip silence here, any voices the OLD song had
     * KEY_ON when the upstream level-load freeze began keep ringing
     * for ~ freeze + RegisterSong I/O ≈ 1-2 s while their KEY_OFFs
     * sit in coalesce_buffer waiting for the next poll.  The kernel
     * MIDI_IOCTL_FLUSH handler zeroes the ring and calls
     * opl_silence_all, which writes KEY_OFF to all 18 voices
     * synchronously inside the ioctl.  AllNotesOff's later KEY_OFFs
     * become redundant on the chip but keep the engine's
     * voice-tracking state in sync; the new song's setup writes
     * (instrument programming + KEY_ONs) overwrite whatever stale
     * register state the silence-and-flush left behind. */
    if (midi_fd >= 0) {
        ioctl(midi_fd, MIDI_IOCTL_FLUSH, 0, 0);
    }
}

void OPL_Delay(uint64_t us) {
    /* No usleep / nanosleep in tools/libc.  Doom's i_oplmusic.c
     * doesn't call this anyway; only the SDL backend uses it for
     * detection-sequence pacing.  Stub it as a no-op rather than a
     * busy-wait — a calibrated busy-wait would be wrong without a
     * cycle counter, and a fixed loop count would be either useless
     * or wildly excessive. */
    (void)us;
}

opl_init_result_t OPL_Detect(void) {
    /* We only ever get here if /dev/midi opened successfully, which
     * means the SB16 OPL3 was probed at boot.  Report OPL3. */
    return OPL_INIT_OPL3;
}

opl_init_result_t OPL_Init(unsigned int port_base) {
    int i;
    (void)port_base;        /* /dev/midi targets the fixed SB16 OPL3 base. */
    midi_fd = open("/dev/midi", O_WRONLY);
    if (midi_fd < 0) {
        return OPL_INIT_NONE;
    }
    music_clock_us = 0;
    clock_anchor_ms = 0;
    anchor_set = 0;
    last_poll_ms = 0;
    last_emitted_ms = 0;
    coalesce_length = 0;
    paused = 0;
    for (i = 0; i < MAX_CALLBACKS; i = i + 1) {
        callbacks[i].in_use = 0;
    }
    return OPL_INIT_OPL3;
}

void OPL_InitRegisters(int opl3) {
    /* Kernel's /dev/midi-open handler already runs opl_silence_all
     * across both banks, so the chip is in a known-zero state when
     * we arrive.  Replay i_oplmusic.c's expected init sequence
     * anyway so callers that re-init mid-session still get a clean
     * slate.  The writes go through the normal OPL_WriteRegister
     * coalescing path. */
    int reg;
    /* Operator + voice registers on bank 0. */
    for (reg = OPL_REGS_TREMOLO; reg < OPL_REGS_TREMOLO + OPL_NUM_OPERATORS; reg = reg + 1) {
        OPL_WriteRegister(reg, 0x00);
    }
    for (reg = OPL_REGS_LEVEL; reg < OPL_REGS_LEVEL + OPL_NUM_OPERATORS; reg = reg + 1) {
        OPL_WriteRegister(reg, 0x3F);
    }
    for (reg = OPL_REGS_ATTACK; reg < OPL_REGS_ATTACK + OPL_NUM_OPERATORS; reg = reg + 1) {
        OPL_WriteRegister(reg, 0x00);
    }
    for (reg = OPL_REGS_SUSTAIN; reg < OPL_REGS_SUSTAIN + OPL_NUM_OPERATORS; reg = reg + 1) {
        OPL_WriteRegister(reg, 0x00);
    }
    for (reg = OPL_REGS_WAVEFORM; reg < OPL_REGS_WAVEFORM + OPL_NUM_OPERATORS; reg = reg + 1) {
        OPL_WriteRegister(reg, 0x00);
    }
    for (reg = OPL_REGS_FREQ_2; reg < OPL_REGS_FREQ_2 + OPL_NUM_VOICES; reg = reg + 1) {
        OPL_WriteRegister(reg, 0x00);
    }
    for (reg = OPL_REGS_FEEDBACK; reg < OPL_REGS_FEEDBACK + OPL_NUM_VOICES; reg = reg + 1) {
        OPL_WriteRegister(reg, 0x00);
    }
    if (opl3) {
        /* Enable OPL3 mode (bank 1, reg 0x05 bit 0 = "new" / OPL3 enable). */
        OPL_WriteRegister(OPL_REG_NEW, 0x01);
        /* Mirror the bank-0 zeroing on bank 1. */
        for (reg = OPL_REGS_TREMOLO; reg < OPL_REGS_TREMOLO + OPL_NUM_OPERATORS; reg = reg + 1) {
            OPL_WriteRegister(reg | 0x100, 0x00);
        }
        for (reg = OPL_REGS_LEVEL; reg < OPL_REGS_LEVEL + OPL_NUM_OPERATORS; reg = reg + 1) {
            OPL_WriteRegister(reg | 0x100, 0x3F);
        }
        for (reg = OPL_REGS_ATTACK; reg < OPL_REGS_ATTACK + OPL_NUM_OPERATORS; reg = reg + 1) {
            OPL_WriteRegister(reg | 0x100, 0x00);
        }
        for (reg = OPL_REGS_SUSTAIN; reg < OPL_REGS_SUSTAIN + OPL_NUM_OPERATORS; reg = reg + 1) {
            OPL_WriteRegister(reg | 0x100, 0x00);
        }
        for (reg = OPL_REGS_WAVEFORM; reg < OPL_REGS_WAVEFORM + OPL_NUM_OPERATORS; reg = reg + 1) {
            OPL_WriteRegister(reg | 0x100, 0x00);
        }
        for (reg = OPL_REGS_FREQ_2; reg < OPL_REGS_FREQ_2 + OPL_NUM_VOICES; reg = reg + 1) {
            OPL_WriteRegister(reg | 0x100, 0x00);
        }
        for (reg = OPL_REGS_FEEDBACK; reg < OPL_REGS_FEEDBACK + OPL_NUM_VOICES; reg = reg + 1) {
            OPL_WriteRegister(reg | 0x100, 0x00);
        }
    }
    /* Enable waveform select (OPL2 backwards-compat). */
    OPL_WriteRegister(OPL_REG_WAVEFORM_ENABLE, 0x20);
    /* Disable rhythm mode + percussion. */
    OPL_WriteRegister(OPL_REG_FM_MODE, 0x00);
}

void OPL_Lock(void) {
    /* No audio thread, no signal handler — nothing to lock. */
}

unsigned int OPL_ReadPort(opl_port_t port) {
    /* Read-back is meaningless over the asynchronous /dev/midi
     * pipe and the upstream music engine never calls this. */
    (void)port;
    return 0;
}

unsigned int OPL_ReadStatus(void) {
    /* Status register polling is part of the OPL detection /
     * timer-IRQ paths the SDL backend uses; the music engine
     * doesn't touch it. */
    return 0;
}

void OPL_SetCallback(uint64_t us, opl_callback_t callback, void *data) {
    int i;
    for (i = 0; i < MAX_CALLBACKS; i = i + 1) {
        if (callbacks[i].in_use) {
            continue;
        }
        callbacks[i].fire_at_us = music_clock_us + us;
        callbacks[i].callback = callback;
        callbacks[i].data = data;
        callbacks[i].in_use = 1;
        return;
    }
    /* Out of slots — drop on the floor.  At 32 slots this only
     * happens on broken MIDI files; the symptom is a stuck note,
     * not a crash. */
    printf("[bboeos doom] OPL_SetCallback: callback table full (>%d active)\n", MAX_CALLBACKS);
}

void OPL_SetPaused(int new_paused) {
    /* On resume, slide clock_anchor_ms forward by however much wall-
     * clock time elapsed during the pause so music_clock_us picks up
     * exactly where it left off (no fast-forward and no re-tempo).
     * A shutdown-before-resume case has anchor_set == 0 and the
     * resume-side branch becomes a no-op; the next poll re-anchors. */
    if (paused && !new_paused && anchor_set) {
        clock_anchor_ms = DG_GetTicksMs() - (uint32_t)(music_clock_us / 1000);
    }
    paused = new_paused;
}

void OPL_SetSampleRate(unsigned int rate) {
    /* The chip generates the audio; we don't synthesise samples. */
    (void)rate;
}

void OPL_Shutdown(void) {
    flush_coalesce();
    if (midi_fd >= 0) {
        close(midi_fd);
        midi_fd = -1;
    }
    coalesce_length = 0;
    OPL_ClearCallbacks();
}

void OPL_Unlock(void) {
    /* Pairs with OPL_Lock — see above. */
}

void OPL_WritePort(opl_port_t port, unsigned int value) {
    /* The upstream music engine in i_oplmusic.c uses
     * OPL_WriteRegister exclusively (verified via grep).  Only the
     * SDL/Allegro detection paths call WritePort directly, and
     * those backends aren't built in our config.  Log + drop if it
     * ever fires so we notice. */
    (void)port;
    (void)value;
    printf("[bboeos doom] OPL_WritePort called unexpectedly (port=%d)\n", (int)port);
}

void OPL_WriteRegister(int reg, int value) {
    uint8_t bank;
    uint8_t register_num;
    uint8_t register_value;
    uint32_t now_ms;
    uint32_t delta_ms;
    uint16_t delay;
    if (midi_fd < 0) {
        return;
    }
    /* Bit 8 of `reg` selects bank 1 on OPL3; lower 8 bits are the
     * register number. */
    bank = (uint8_t)((reg >> 8) & 0x01);
    register_num = (uint8_t)(reg & 0xFF);
    register_value = (uint8_t)(value & 0xFF);
    now_ms = (uint32_t)(music_clock_us / 1000);
    if (now_ms < last_emitted_ms) {
        /* Should not happen — clock only advances forward — but
         * guard against integer-overflow corner cases. */
        delta_ms = 0;
    } else {
        delta_ms = now_ms - last_emitted_ms;
    }
    if (delta_ms > 0xFFFF) {
        /* /dev/midi's delay field is 16-bit ms.  Emit a stretch of
         * silence-bank "no-op" commands to walk past the gap.  In
         * practice MUS events never have multi-minute idle gaps
         * with the music engine running, so this is paranoia. */
        while (delta_ms > 0xFFFF) {
            append_command(0xFFFF, 2, 0, 0);    /* bank=2 → kernel drops, clock still advances */
            delta_ms = delta_ms - 0xFFFF;
        }
    }
    delay = (uint16_t)delta_ms;
    append_command(delay, bank, register_num, register_value);
    last_emitted_ms = now_ms;
}

static void append_command(uint16_t delay, uint8_t bank, uint8_t reg, uint8_t value) {
    uint8_t *slot;
    if (coalesce_length + COMMAND_BYTES > COALESCE_BYTES) {
        flush_coalesce();
    }
    slot = coalesce_buffer + coalesce_length;
    slot[0] = (uint8_t)(delay & 0xFF);
    slot[1] = (uint8_t)((delay >> 8) & 0xFF);
    slot[2] = bank;
    slot[3] = reg;
    slot[4] = value;
    slot[5] = 0;
    coalesce_length = coalesce_length + COMMAND_BYTES;
}

static int find_next_due_callback(uint64_t now_us) {
    int best = -1;
    int i;
    for (i = 0; i < MAX_CALLBACKS; i = i + 1) {
        if (!callbacks[i].in_use) {
            continue;
        }
        if (callbacks[i].fire_at_us > now_us) {
            continue;
        }
        if (best < 0 || callbacks[i].fire_at_us < callbacks[best].fire_at_us) {
            best = i;
        }
    }
    return best;
}

static void flush_coalesce(void) {
    int written;
    int total = 0;
    if (midi_fd < 0 || coalesce_length == 0) {
        return;
    }
    while (total < coalesce_length) {
        written = (int)write(midi_fd, coalesce_buffer + total, (size_t)(coalesce_length - total));
        if (written <= 0) {
            /* Kernel ring full (short write returns 0 partway
             * through) or an error — drop the rest rather than
             * spin.  The next poll will pick up where we left off
             * with fresh delay deltas. */
            break;
        }
        total = total + written;
    }
    coalesce_length = 0;
}

/* opl_bboeos_poll — BBoeOS-specific backend pulse, NOT part of
 * opl.h.  Called once per frame from BBoe_MusicPoll (via doomgeneric's
 * S_UpdateSounds → I_UpdateSound → music_module->Poll path).
 *
 * Compute wall_now_us as wall-clock-elapsed-since-init.  Use it as the
 * "is this callback due?" threshold (so frame-rate dips don't change
 * which callbacks fire), but snap music_clock_us to each individual
 * callback's fire_at_us before invoking it.  That snap is what makes
 * OPL_WriteRegister produce the correct *inter-event* delay: each
 * delay = (this_callback_fire_us - prev_callback_fire_us) / 1000.
 *
 * Without the per-callback snap, every event in a single poll burst
 * sees the same music_clock_us, so the first event carries the whole
 * accumulated delay and every subsequent event in that burst encodes
 * delay = 0.  The kernel ring then drains them at 16 events/ms back-
 * to-back, collapsing any music intended to span tens of ms within a
 * single poll into a tight burst — i.e. "music plays slow" with a
 * stutter pattern that follows the poll cadence.
 *
 * After all due callbacks fire we leave music_clock_us == wall_now_us
 * so OPL_SetCallback called *outside* the poll context (e.g. during
 * I_OPL_PlaySong) anchors against wall time.  When paused the clock
 * doesn't advance and callbacks won't fire — but we still flush any
 * leftover bytes (none expected). */
void opl_bboeos_poll(void) {
    int slot;
    uint32_t now_ms;
    uint32_t gap_ms;
    uint64_t wall_now_us;
    if (midi_fd < 0) {
        return;
    }
    now_ms = DG_GetTicksMs();
    if (!paused) {
        if (!anchor_set) {
            clock_anchor_ms = now_ms;
            anchor_set = 1;
        } else {
            /* Detect a long gap since the previous poll (level load,
             * tally screen, blocking I/O — anything > FREEZE_GAP_MS).
             * Slide the anchor forward by the entire gap so
             * music_clock_us doesn't jump and the due-callback loop
             * doesn't unleash a hundreds-of-events burst that would
             * overflow the 64-event coalesce buffer + 256-event
             * kernel ring.  The song effectively pauses through the
             * stall and resumes from where it left off. */
            gap_ms = now_ms - last_poll_ms;
            if (gap_ms > FREEZE_GAP_MS) {
                clock_anchor_ms = clock_anchor_ms + gap_ms;
            }
        }
        wall_now_us = (uint64_t)(now_ms - clock_anchor_ms) * 1000ULL;
        for (;;) {
            slot = find_next_due_callback(wall_now_us);
            if (slot < 0) {
                break;
            }
            /* Snap music_clock_us to this callback's intended fire
             * time.  Don't go backward — the queue can hand us a
             * callback whose fire_at_us is older than music_clock_us
             * if a prior callback in this batch scheduled past-due
             * work; clamp to monotonic so OPL_WriteRegister's delta
             * calculation (now_ms - last_emitted_ms) stays valid. */
            if (callbacks[slot].fire_at_us > music_clock_us) {
                music_clock_us = callbacks[slot].fire_at_us;
            }
            callbacks[slot].in_use = 0;
            callbacks[slot].callback(callbacks[slot].data);
        }
        if (wall_now_us > music_clock_us) {
            music_clock_us = wall_now_us;
        }
    }
    /* Always update last_poll_ms — even when paused — so the first
     * post-resume poll sees gap_ms ≈ 0 and the freeze-detection
     * branch doesn't fire on top of OPL_SetPaused's own anchor
     * slide.  (Belt and braces: OPL_SetPaused already re-anchors on
     * resume; both paths converge on "music continues from where it
     * paused.") */
    last_poll_ms = now_ms;
    flush_coalesce();
}
