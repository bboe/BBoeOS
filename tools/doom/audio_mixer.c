// tools/doom/audio_mixer.c — see audio_mixer.h for the API contract.

#include "audio_mixer.h"

struct voice {
    const uint8_t *samples;
    int length;
    int offset;
    int volume;     // 0..255
    int active;
};

static struct voice voices[MIXER_VOICE_COUNT];

void mixer_render(uint8_t *destination, int sample_count) {
    int sample_index = 0;
    while (sample_index < sample_count) {
        int accumulator = 128;
        int voice_index = 0;
        while (voice_index < MIXER_VOICE_COUNT) {
            struct voice *voice = &voices[voice_index];
            if (voice->active) {
                int raw = (int)voice->samples[voice->offset];
                int delta = raw - 128;
                int scaled = (delta * voice->volume) / 255;
                accumulator = accumulator + scaled;
                voice->offset = voice->offset + 1;
                if (voice->offset >= voice->length) {
                    voice->active = 0;
                }
            }
            voice_index = voice_index + 1;
        }
        if (accumulator < 0) {
            accumulator = 0;
        }
        if (accumulator > 255) {
            accumulator = 255;
        }
        destination[sample_index] = (uint8_t)accumulator;
        sample_index = sample_index + 1;
    }
}

void mixer_reset(void) {
    int index = 0;
    while (index < MIXER_VOICE_COUNT) {
        voices[index].samples = 0;
        voices[index].length = 0;
        voices[index].offset = 0;
        voices[index].volume = 0;
        voices[index].active = 0;
        index = index + 1;
    }
}

int mixer_start_voice(int voice_index, const uint8_t *samples, int length, int volume) {
    if (voice_index < 0 || voice_index >= MIXER_VOICE_COUNT) {
        return 0;
    }
    if (samples == 0 || length <= 0) {
        return 0;
    }
    voices[voice_index].samples = samples;
    voices[voice_index].length = length;
    voices[voice_index].offset = 0;
    voices[voice_index].volume = volume;
    voices[voice_index].active = 1;
    return 1;
}

void mixer_stop_voice(int voice_index) {
    if (voice_index < 0 || voice_index >= MIXER_VOICE_COUNT) {
        return;
    }
    voices[voice_index].active = 0;
}

int mixer_voice_active(int voice_index) {
    if (voice_index < 0 || voice_index >= MIXER_VOICE_COUNT) {
        return 0;
    }
    return voices[voice_index].active;
}
