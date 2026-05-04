/* tools/doom/include/SDL_mixer.h — empty shim.
 *
 * doomgeneric's i_sound.c does `#include <SDL_mixer.h>` under
 * `#if defined(FEATURE_SOUND) && !defined(__DJGPP__)`.  We compile
 * with -DFEATURE_SOUND so doomgeneric registers our DG_sound_module,
 * but the file otherwise doesn't reference any SDL_mixer symbols —
 * the include is dead weight in this configuration.  An empty shim
 * lets the compile proceed without pulling in the real library. */
