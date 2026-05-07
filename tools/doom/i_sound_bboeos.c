/* tools/doom/i_sound_bboeos.c — bboeos backend for doomgeneric's
 * sound_module_t / music_module_t.
 *
 * Provides DG_sound_module + DG_music_module (referenced by
 * doomgeneric/i_sound.c when built with -DFEATURE_SOUND).  The sound
 * module opens /dev/audio at init, mixes Doom's voices through the
 * 8-channel software mixer in audio_mixer.c, and writes one tick's
 * worth of mixed PCM (~315 samples at 11025 Hz / 35 Hz) per Update
 * call.  Music is delegated to chocolate-doom's i_oplmusic.c via the
 * upstream music_opl_module struct (its I_OPL_* functions are static,
 * so the only legal entry point is through the function-pointer
 * table).  The Poll slot is the BBoeOS-specific opl_bboeos_poll
 * extension (upstream's engine is callback-driven and has no Poll).
 *
 * Function definitions are in alphabetical order (see CLAUDE.md);
 * the two module-struct definitions sit at the bottom because they
 * reference every BBoe_* function.
 *
 * WAD sound format (per i_sdlsound.c CacheSFX):
 *   bytes 0-1 = 0x03, 0x00  (DMX format ID)
 *   bytes 2-3 = sample rate (LE 16-bit)
 *   bytes 4-7 = sample count (LE 32-bit; should match lumplen - 8)
 *   bytes 8..23 = 16-byte leading pad
 *   bytes 24..end-16 = actual 8-bit unsigned PCM samples
 *   last 16 bytes = trailing pad
 *
 * This backend ignores the WAD's per-sample rate field — Doom's
 * canonical SFX are 11025 Hz mono unsigned 8-bit, exactly what
 * /dev/audio takes.  The handful at 8000/22050 Hz play at the wrong
 * pitch; that's acceptable for Phase A. */

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

#include "audio_mixer.h"
#include "doomtype.h"
#include "i_sound.h"
#include "w_wad.h"
#include "z_zone.h"

#define AUDIO_IOCTL_QUERY 0x00
#define TICK_SAMPLES      315  /* 11025 / 35 ≈ 315 samples per Doom tick */

/* Tunables i_sound.c's I_BindSoundVariables references — normally
 * defined in i_sdlsound.c / i_allegrosound.c, both of which we
 * exclude from the build.  Provide stubs so linking succeeds; we
 * don't actually use libsamplerate. */
int use_libsamplerate = 0;
float libsamplerate_scale = 1.0f;

static int audio_fd = -1;
static int audio_ok = 0;
static int next_voice = 0;

/* Round-robin voice slot selection.  Doom's sound queue (s_sound.c)
 * already manages priority + drop-the-oldest logic before calling
 * StartSound, so we just rotate. */
static int allocate_voice(void) {
    int chosen = next_voice;
    next_voice = (next_voice + 1) % MIXER_VOICE_COUNT;
    return chosen;
}

static void BBoe_CacheSounds(sfxinfo_t *sounds, int num_sounds) {
    (void)sounds;
    (void)num_sounds;
}

static int BBoe_GetSfxLumpNum(sfxinfo_t *sfx) {
    /* DS-prefix prepended for Doom-style sound names. */
    char name[9];
    int i;
    name[0] = 'd';
    name[1] = 's';
    i = 0;
    while (i < 6 && sfx->name[i] != 0) {
        name[2 + i] = sfx->name[i];
        i = i + 1;
    }
    name[2 + i] = 0;
    return W_GetNumForName(name);
}

static boolean BBoe_Init(boolean use_sfx_prefix) {
    (void)use_sfx_prefix;
    audio_fd = open("/dev/audio", O_WRONLY);
    if (audio_fd < 0) {
        printf("[bboeos doom] /dev/audio unavailable; SFX disabled\n");
        audio_ok = 0;
        return false;
    }
    audio_ok = 1;
    mixer_reset();
    printf("[bboeos doom] /dev/audio fd=%d, SFX enabled\n", audio_fd);
    return true;
}

/* Music: delegated to chocolate-doom's music_opl_module (i_oplmusic.c).
 * The upstream I_OPL_* functions are file-static; the only public
 * entry point is the music_opl_module function-pointer table.
 *
 * Chocolate 3.1.0's i_oplmusic.c defines that table as
 * `const music_module_t music_opl_module`, while doomgeneric's older
 * i_sound.h has a non-const `extern music_module_t music_opl_module;`.
 * We accept the doomgeneric extern here — the cv-qualifier mismatch is
 * invisible at link time on this i386 target — and let the chocolate
 * TU dodge the colliding extern via tools/doom/chocolate_compat.h
 * (the shim pre-includes i_sound.h with the symbol renamed to a
 * sacrificial name so chocolate's definition wins).
 *
 * BBoe_MusicPoll calls opl_bboeos_poll() — the BBoeOS extension in
 * opl_bboeos.c — instead of an upstream slot because chocolate-doom's
 * engine is callback-driven and does not export a poll function. */
extern void opl_bboeos_poll(void);

static boolean BBoe_MusicInit(void) {
    boolean ok = music_opl_module.Init();
    if (ok) {
        printf("[bboeos doom] OPL music enabled\n");
    } else {
        printf("[bboeos doom] OPL music unavailable\n");
    }
    return ok;
}

static boolean BBoe_MusicIsPlaying(void) {
    return music_opl_module.MusicIsPlaying();
}

static void BBoe_MusicPause(void) {
    music_opl_module.PauseMusic();
}

static void BBoe_MusicPlaySong(void *handle, boolean looping) {
    music_opl_module.PlaySong(handle, looping);
}

static void BBoe_MusicPoll(void) {
    opl_bboeos_poll();
}

static void *BBoe_MusicRegisterSong(void *data, int len) {
    return music_opl_module.RegisterSong(data, len);
}

static void BBoe_MusicResume(void) {
    music_opl_module.ResumeMusic();
}

static void BBoe_MusicSetVolume(int volume) {
    music_opl_module.SetMusicVolume(volume);
}

static void BBoe_MusicShutdown(void) {
    music_opl_module.Shutdown();
}

static void BBoe_MusicStopSong(void) {
    music_opl_module.StopSong();
}

static void BBoe_MusicUnRegisterSong(void *handle) {
    music_opl_module.UnRegisterSong(handle);
}

static void BBoe_Shutdown(void) {
    if (audio_fd >= 0) {
        close(audio_fd);
        audio_fd = -1;
        audio_ok = 0;
    }
}

static boolean BBoe_SoundIsPlaying(int channel) {
    if (!audio_ok) {
        return false;
    }
    return mixer_voice_active(channel) ? true : false;
}

static int BBoe_StartSound(sfxinfo_t *sfxinfo, int channel, int vol, int sep) {
    int lumpnum;
    int lumplen;
    uint8_t *data;
    int sample_length;
    int voice;
    (void)channel;
    (void)sep;
    if (!audio_ok) {
        return -1;
    }
    if (sfxinfo->lumpnum < 0) {
        sfxinfo->lumpnum = BBoe_GetSfxLumpNum(sfxinfo);
    }
    lumpnum = sfxinfo->lumpnum;
    if (lumpnum < 0) {
        return -1;
    }
    data = (uint8_t *)W_CacheLumpNum(lumpnum, PU_STATIC);
    lumplen = (int)W_LumpLength(lumpnum);
    if (data == NULL || lumplen <= 32) {
        return -1;
    }
    /* DMX header check: 0x03 0x00 at lump start. */
    if (data[0] != 0x03 || data[1] != 0x00) {
        return -1;
    }
    sample_length = lumplen - 32;
    voice = allocate_voice();
    /* vol from Doom is 0..127; scale to mixer's 0..255 range. */
    mixer_start_voice(voice, data + 24, sample_length, vol * 2);
    return voice;
}

static void BBoe_StopSound(int channel) {
    if (audio_ok) {
        mixer_stop_voice(channel);
    }
}

static void BBoe_Update(void) {
    uint8_t buffer[TICK_SAMPLES];
    if (!audio_ok) {
        return;
    }
    mixer_render(buffer, TICK_SAMPLES);
    write(audio_fd, buffer, TICK_SAMPLES);
}

static void BBoe_UpdateSoundParams(int channel, int volume, int separation) {
    (void)channel;
    (void)volume;
    (void)separation;
    /* Single-shot voices in v1; per-channel volume changes after start
     * not supported.  Doom mostly uses this for distance attenuation
     * and stereo separation, neither of which is implemented yet. */
}

/* Doomgeneric's SNDDEVICE_SB matches snd_sfxdevice's default in
 * i_sound.c, so this is the module that gets selected without any
 * config change. */
static snddevice_t bboe_sound_devices[] = {
    SNDDEVICE_SB,
    SNDDEVICE_SB,
};

sound_module_t DG_sound_module = {
    bboe_sound_devices,
    sizeof(bboe_sound_devices) / sizeof(*bboe_sound_devices),
    BBoe_Init,
    BBoe_Shutdown,
    BBoe_GetSfxLumpNum,
    BBoe_Update,
    BBoe_UpdateSoundParams,
    BBoe_StartSound,
    BBoe_StopSound,
    BBoe_SoundIsPlaying,
    BBoe_CacheSounds,
};

music_module_t DG_music_module = {
    NULL,                       /* sound_devices */
    0,                          /* num_sound_devices */
    BBoe_MusicInit,
    BBoe_MusicShutdown,
    BBoe_MusicSetVolume,
    BBoe_MusicPause,
    BBoe_MusicResume,
    BBoe_MusicRegisterSong,
    BBoe_MusicUnRegisterSong,
    BBoe_MusicPlaySong,
    BBoe_MusicStopSong,
    BBoe_MusicIsPlaying,
    BBoe_MusicPoll,
};
