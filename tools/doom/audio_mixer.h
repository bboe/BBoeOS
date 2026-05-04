// tools/doom/audio_mixer.h — 8-voice software mixer used by
// i_sound_bboeos.c to mix Doom SFX into a single 8-bit unsigned PCM
// stream for /dev/audio.
//
// All samples are 8-bit unsigned (Doom's WAD samples and SB16's 8-bit
// DMA format match exactly).  Midpoint = 128.  The mixer accumulates
// signed deviations from midpoint into an int per output sample,
// applies per-voice volume scaling, then clamps to [0, 255] when
// converting back.
//
// Pure C — no syscalls, no globals owned by anyone else.  Unit-tested
// in tests/unit/test_audio_mixer.py against host clang.

#ifndef BBOEOS_AUDIO_MIXER_H
#define BBOEOS_AUDIO_MIXER_H

#include <stdint.h>

#define MIXER_VOICE_COUNT 8

void mixer_render(uint8_t *destination, int sample_count);
void mixer_reset(void);
int  mixer_start_voice(int voice_index, const uint8_t *samples, int length, int volume);
void mixer_stop_voice(int voice_index);
int  mixer_voice_active(int voice_index);

#endif
