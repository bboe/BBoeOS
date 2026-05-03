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

#include "doomkeys.h"
#include "doomgeneric.h"

/* Kernel ABI constants we hand to ioctl + the user-virt slot the kernel
 * maps mode-13h's framebuffer into via SYS_VIDEO_MAP.  Mirrored from
 * src/include/constants.asm so the user-side build doesn't need a
 * kernel-header pull. */
#define MODE13H_BYTES               (320 * 200)
#define MODE13H_USER_VIRT           0xB8000000u
#define VGA_IOCTL_MODE              0x01
#define VIDEO_MODE_VGA_320x200_256  0x13

static uint8_t *vga_framebuffer = NULL;

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
    memcpy(vga_framebuffer, DG_ScreenBuffer, MODE13H_BYTES);
}

int DG_GetKey(int *pressed, unsigned char *key) {
    (void)pressed; (void)key;
    return 0;
}

uint32_t DG_GetTicksMs(void) {
    /* SYS_RTC_MILLIS returns DX:AX = ms since boot.  Recompose into a
     * full uint32_t so doomgeneric_Tick sees a monotonic millisecond
     * counter (wraps at ~49.7 days; far past any realistic Doom session). */
    unsigned int ms_lo, ms_hi;
    __asm__ volatile (
        "mov $0x31, %%ah\n\t"               /* SYS_RTC_MILLIS */
        "int $0x30\n\t"
        : "=a"(ms_lo), "=d"(ms_hi));
    return (ms_hi << 16) | (ms_lo & 0xFFFF);
}

void DG_Init(void) {
    printf("[bboeos doom] DG_Init\n");
    /* Switch the VGA card into mode 13h (320x200 8-bit) and map its
     * framebuffer into our address space.  After this, DG_DrawFrame
     * can blit DG_ScreenBuffer (palette indices, thanks to -DCMAP256)
     * straight into vga_framebuffer with a 64000-byte memcpy. */
    int vga_fd = open("/dev/vga", O_WRONLY);
    if (vga_fd < 0) {
        printf("[bboeos doom] /dev/vga open failed\n");
        return;
    }
    ioctl(vga_fd, VGA_IOCTL_MODE, 0, VIDEO_MODE_VGA_320x200_256);
    close(vga_fd);
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
    /* SYS_RTC_SLEEP takes CX (16 bits) so cap one syscall at 65535 ms;
     * Doom sleeps small intervals (1–16 ms for frame pacing) so this
     * loop almost never iterates more than once. */
    while (ms > 0) {
        unsigned short chunk = (unsigned short)(ms > 0xFFFF ? 0xFFFF : ms);
        __asm__ volatile (
            "mov %[ms], %%cx\n\t"
            "mov $0x32, %%ah\n\t"           /* SYS_RTC_SLEEP */
            "int $0x30\n\t"
            : : [ms]"r"(chunk) : "ax", "cx");
        ms -= chunk;
    }
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
