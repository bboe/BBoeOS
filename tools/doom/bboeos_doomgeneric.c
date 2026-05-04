/* tools/doom/bboeos_doomgeneric.c — bboeos backend for doomgeneric.
 *
 * Implements the DG_* abstraction (display, input, time) on top of
 * the bboeos kernel ABI: SYS_VIDEO_MAP for the MODE13H VGA buffer,
 * SYS_RTC_* for tick counting, fd-0 read for keyboard input.
 *
 * Phase A target is just to get the engine ticking — DG_DrawFrame
 * stubs to a serial-marker print so we can verify in QEMU that the
 * main loop is alive.  Real VGA blit + key conversion lands later
 * in this file. */

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <bbkeys.h>

#include "doomgeneric.h"
#include "doomkeys.h"
#include "doomtype.h"
#include "i_video.h"

/* Kernel ABI constants we hand to ioctl + the user-virt slot the kernel
 * maps mode-13h's framebuffer into via SYS_VIDEO_MAP.  Mirrored from
 * src/include/constants.asm so the user-side build doesn't need a
 * kernel-header pull. */
#define CONSOLE_IOCTL_TRY_GETC      0x00
#define CONSOLE_IOCTL_TRY_GET_EVENT 0x01
#define MODE13H_BYTES               (320 * 200)
#define MODE13H_USER_VIRT           0xB8000000u
#define VGA_IOCTL_MODE              0x01
#define VGA_IOCTL_SET_PALETTE       0x02
#define VIDEO_MODE_VGA_320x200_256  0x13

static int vga_fd = -1;
static uint8_t *vga_framebuffer = NULL;

/* Tiny ring buffer of (pressed << 8 | key) entries.  DG_GetKey drains
 * the kernel keyboard buffer each call and synthesises a press / release
 * pair per ASCII byte — Doom only needs taps for menus / use / fire,
 * and "hold to walk forward" works via repeat presses. */
#define KQ_SIZE 16
static unsigned short kq[KQ_SIZE];
static int kq_head = 0;
static int kq_tail = 0;

static void kq_push(int pressed, unsigned char key) {
    int next = (kq_tail + 1) % KQ_SIZE;
    if (next == kq_head) return;    /* drop on overflow */
    kq[kq_tail] = (unsigned short)((pressed << 8) | key);
    kq_tail = next;
}

static int kq_pop(int *pressed, unsigned char *key) {
    if (kq_head == kq_tail) return 0;
    unsigned short e = kq[kq_head];
    kq_head = (kq_head + 1) % KQ_SIZE;
    *pressed = (e >> 8) & 1;
    *key = (unsigned char)(e & 0xFF);
    return 1;
}

/* BBKEY_* (positional kernel key code) -> Doom keycode.  Covers
 * keyboard-only play: WASD for movement, arrows for the same,
 * Ctrl/F for fire, Space/E for use, plus enter / esc / tab /
 * digits / yes-no for menus and confirmation prompts.  Returns 0
 * for codes we don't bind.
 *
 * Arrow keys, modifiers, and standalone keys all arrive as single
 * (pressed, BBKEY_*) events now — no CSI sequence reassembly, no
 * ASCII synthesis hacks.  See tools/libc/include/bbkeys.h for the
 * full code list and src/drivers/ps2.c for the producer side. */
static unsigned char keycode_to_doom(int code) {
    switch (code) {
        case BBKEY_W:        return KEY_UPARROW;
        case BBKEY_A:        return KEY_LEFTARROW;
        case BBKEY_S:        return KEY_DOWNARROW;
        case BBKEY_D:        return KEY_RIGHTARROW;
        case BBKEY_UP:       return KEY_UPARROW;
        case BBKEY_DOWN:     return KEY_DOWNARROW;
        case BBKEY_LEFT:     return KEY_LEFTARROW;
        case BBKEY_RIGHT:    return KEY_RIGHTARROW;
        case BBKEY_LCTRL:    return KEY_FIRE;
        case BBKEY_RCTRL:    return KEY_FIRE;
        case BBKEY_F:        return KEY_FIRE;     /* serial-terminal fallback */
        case BBKEY_SPACE:    return KEY_USE;
        case BBKEY_E:        return KEY_USE;
        case BBKEY_COMMA:    return KEY_STRAFE_L;
        case BBKEY_PERIOD:   return KEY_STRAFE_R;
        case BBKEY_ENTER:    return KEY_ENTER;
        case BBKEY_ESC:      return KEY_ESCAPE;
        case BBKEY_TAB:      return KEY_TAB;
        case BBKEY_Y:        return 'y';          /* confirm prompts */
        case BBKEY_N:        return 'n';
        case BBKEY_0:        return '0';
        case BBKEY_1:        return '1';
        case BBKEY_2:        return '2';
        case BBKEY_3:        return '3';
        case BBKEY_4:        return '4';
        case BBKEY_5:        return '5';
        case BBKEY_6:        return '6';
        case BBKEY_7:        return '7';
        case BBKEY_8:        return '8';
        case BBKEY_9:        return '9';
        default:             return 0;
    }
}

static uint32_t sys_video_map(void) {
    /* SYS_VIDEO_MAP: maps the mode-13h FB into our PD at MODE13H_USER_VIRT.
     * Returns EAX = the virt address, CF=1 / EAX=-1 on PT-allocation failure. */
    uint32_t va;
    __asm__ volatile (
        "mov $0x40, %%ah\n\t"           /* SYS_VIDEO_MAP */
        "int $0x30\n\t"
        : "=a"(va));
    return va;
}

void DG_DrawFrame(void) {
    /* DG_ScreenBuffer is 320x200 palette indices (because we built with
     * -DCMAP256 + DOOMGENERIC_RES{X,Y}=320,200), and so is the VGA
     * mode-13h framebuffer — one memcpy hands the engine's frame
     * straight to the screen.  Skip if SYS_VIDEO_MAP failed at init. */
    if (vga_framebuffer == NULL) return;
    /* Re-program the VGA DAC when Doom updates its palette (status-bar
     * damage flash, gamma change, level transitions).  Doom stores RGB
     * as 8-bit; the DAC is 6-bit so we shift right by 2.  256 ioctls
     * per palette change is acceptable — it happens 1-2 times per
     * scene, not per frame. */
    if (palette_changed && vga_fd >= 0) {
        for (int i = 0; i < 256; i++) {
            unsigned int cx_arg = ((unsigned int)(colors[i].r >> 2) << 8) | (unsigned int)i;
            unsigned int dx_arg = ((unsigned int)(colors[i].b >> 2) << 8) | (unsigned int)(colors[i].g >> 2);
            ioctl(vga_fd, VGA_IOCTL_SET_PALETTE, cx_arg, dx_arg);
        }
        palette_changed = false;
    }
    memcpy(vga_framebuffer, DG_ScreenBuffer, MODE13H_BYTES);
    /* Serial-marker the first few frames so the smoke test can confirm
     * the engine main loop is actually ticking and our blit ran.  */
    static unsigned int frames = 0;
    frames++;
    if (frames <= 3 || frames == 30) {
        printf("[bboeos doom] frame %u\n", frames);
    }
}

int DG_GetKey(int *pressed, unsigned char *key) {
    /* Drain the kernel's per-fd PS/2 event ring.  Each
     * CONSOLE_IOCTL_TRY_GET_EVENT call returns one
     *
     *     (pressed << 16) | bbkey       (real event)
     *     0                               (empty queue)
     *
     * with bbkey one of the BBKEY_* codes from <bbkeys.h>.  The
     * codes are positional and one-per-key (arrows, modifiers, and
     * regular keys all surface as a single event), so the engine
     * sees clean keydown / keyup pairs without needing to reassemble
     * any CSI sequences or invent synthesized bytes for modifiers. */
    for (;;) {
        int event = ioctl(0, CONSOLE_IOCTL_TRY_GET_EVENT, 0, 0);
        if (event <= 0) break;
        int p = (event >> 16) & 1;
        int code = event & 0xFFFF;
        unsigned char k = keycode_to_doom(code);
        if (k != 0) {
            kq_push(p, k);
        }
    }
    return kq_pop(pressed, key);
}

uint32_t DG_GetTicksMs(void) {
    /* SYS_RTC_MILLIS returns EAX = ms since boot, monotonic, wraps at
     * ~49.7 days (far past any realistic Doom session). */
    unsigned int ms;
    __asm__ volatile (
        "mov $0x31, %%ah\n\t"               /* SYS_RTC_MILLIS */
        "int $0x30\n\t"
        : "=a"(ms));
    return ms;
}

void DG_Init(void) {
    printf("[bboeos doom] DG_Init\n");
    /* Switch the VGA card into mode 13h (320x200 8-bit) and map its
     * framebuffer into our address space.  After this, DG_DrawFrame
     * can blit DG_ScreenBuffer (palette indices, thanks to -DCMAP256)
     * straight into vga_framebuffer with a 64000-byte memcpy. */
    /* Keep vga_fd open for the lifetime of the program — DG_DrawFrame
     * uses it to push palette updates via VGA_IOCTL_SET_PALETTE, so
     * closing here would mean reopening every palette change. */
    vga_fd = open("/dev/vga", O_WRONLY);
    if (vga_fd < 0) {
        printf("[bboeos doom] /dev/vga open failed\n");
        return;
    }
    ioctl(vga_fd, VGA_IOCTL_MODE, 0, VIDEO_MODE_VGA_320x200_256);
    uint32_t va = sys_video_map();
    if (va == 0xFFFFFFFFu) {
        printf("[bboeos doom] SYS_VIDEO_MAP failed\n");
        return;
    }
    vga_framebuffer = (uint8_t *)va;
    printf("[bboeos doom] VGA mapped at %p\n", vga_framebuffer);
}

void DG_SetWindowTitle(const char *title) {
    (void)title;
}

void DG_SleepMs(uint32_t ms) {
    /* SYS_RTC_SLEEP takes ECX (full 32-bit ms count). */
    if (ms == 0) return;
    __asm__ volatile (
        "mov %[ms], %%ecx\n\t"
        "mov $0x32, %%ah\n\t"               /* SYS_RTC_SLEEP */
        "int $0x30\n\t"
        : : [ms]"r"(ms) : "ax", "ecx");
}

extern void doomgeneric_Create(int argc, char **argv);
extern void doomgeneric_Tick(void);

int main(int argc, char **argv) {
    /* Phase A: hand doomgeneric a fixed argv pointing at the WAD path
     * we drop on the disk image.  The libc _start stub passes argc=1
     * with argv={"", NULL} so the user's typed command line is ignored
     * for now — the WAD location is hard-coded. */
    (void)argc; (void)argv;
    static char *fake_argv[] = {(char *)"doom", (char *)"-iwad", (char *)"doom1.wad", NULL};
    doomgeneric_Create(3, fake_argv);
    for (;;) {
        doomgeneric_Tick();
    }
    return 0;
}
