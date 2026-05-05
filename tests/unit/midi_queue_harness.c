/* tests/unit/midi_queue_harness.c — host-clang harness for the
 * /dev/midi event queue + drain logic.
 *
 * The kernel sources `src/fs/fd/midi.c` and `src/drivers/opl3.c` are
 * cc.py-targeted: they use `__attribute__((carry_return))`, register
 * pinning attributes, NASM-syntax `asm("foo equ _g_foo")` aliases, and
 * the `kernel_outb` / `kernel_inb` cc.py intrinsics.  This harness
 * compiles a stripped copy of those sources (see
 * tests/unit/test_midi_queue.py for the preprocessing pass) against
 * the host system clang so we can exercise the pure-C ring + drain
 * logic without booting QEMU.
 *
 * Tested surface (B8 plan, approach 1):
 *     midi_reset_state, midi_ring_full, midi_ring_push, midi_drain_due,
 *     fd_close_midi, fd_write_midi (the carry_return attribute is
 *     erased, so it returns int; the test driver ignores that).
 *
 * Deferred to integration testing (play_midi smoke + Doom music):
 *     fd_ioctl_midi — implemented in inline NASM, can't compile here.
 *     The opl_probe / kernel_inb 0x388 status handshake — exercised
 *     from QEMU only.
 *
 * I/O capture: the harness redefines `kernel_outb` to append to a
 * fixed-size record buffer and `kernel_inb` to a stub returning 0.
 * Test code reads back the recorded (port, value) pairs to confirm
 * exactly which OPL writes the kernel emitted.
 */

#include <stddef.h>
#include <stdint.h>

/* The kernel C files reference these globals as `extern`; midi.c
 * declares them at file scope.  opl3.c owns the storage for
 * `opl3_present`, so the harness only needs to provide
 * `system_ticks` and `fd_write_buffer` (both are externs in
 * midi.c). */
uint8_t *fd_write_buffer;
extern uint8_t opl3_present;
uint32_t system_ticks;

/* I/O recorder.  A capacity of 4096 entries comfortably covers all
 * test cases (the largest emits 18 outb pairs from opl_silence_all
 * → 36 entries; the 256-event test enqueues at most 255 outbs). */
#define HARNESS_MAX_RECORDS 4096

struct harness_io_record {
    int is_inb;       /* 1 when this slot recorded a kernel_inb call */
    int port;
    int value;        /* outb value; for inb, the (always-zero) returned byte */
};

static struct harness_io_record harness_records[HARNESS_MAX_RECORDS];
static int harness_record_count;

/* kernel_inb / kernel_outb stand-ins.  In the real kernel these are
 * cc.py intrinsics that emit `out`/`in` directly; here we make them
 * ordinary C functions so the test can verify the kernel's call
 * sequence. */
int kernel_inb(int port) {
    if (harness_record_count < HARNESS_MAX_RECORDS) {
        harness_records[harness_record_count].is_inb = 1;
        harness_records[harness_record_count].port = port;
        harness_records[harness_record_count].value = 0;
        harness_record_count = harness_record_count + 1;
    }
    return 0;
}

void kernel_outb(int port, int value) {
    if (harness_record_count < HARNESS_MAX_RECORDS) {
        harness_records[harness_record_count].is_inb = 0;
        harness_records[harness_record_count].port = port;
        harness_records[harness_record_count].value = value;
        harness_record_count = harness_record_count + 1;
    }
}

/* Symbols the test driver reaches through ctypes.  The ring + indices
 * live inside the preprocessed midi.c, but we expose accessor
 * functions so the test never has to know the host-side layout of
 * `struct midi_event`. */

extern uint8_t midi_head;
extern uint8_t midi_tail;
extern uint32_t midi_virtual_clock;

uint8_t harness_midi_head(void) {
    return midi_head;
}

uint8_t harness_midi_tail(void) {
    return midi_tail;
}

uint32_t harness_midi_virtual_clock(void) {
    return midi_virtual_clock;
}

int harness_record_count_get(void) {
    return harness_record_count;
}

int harness_record_is_inb(int index) {
    return harness_records[index].is_inb;
}

int harness_record_port(int index) {
    return harness_records[index].port;
}

int harness_record_value(int index) {
    return harness_records[index].value;
}

void harness_reset_records(void) {
    harness_record_count = 0;
}

/* The kernel sources expect the cc.py-specific attribute syntax to be
 * silently consumed.  Erase it before the preprocessor sees the
 * stripped copies. */
#define __attribute__(x)

/* Pull in the (Python-stripped) kernel sources.  test_midi_queue.py
 * writes `midi_stripped.c` and `opl3_stripped.c` to the same temp
 * directory as this harness, then asks clang to compile them
 * alongside.  We declare the prototypes here so this harness can call
 * them from accessor wrappers if needed; the linker resolves the
 * definitions from those translation units. */

void fd_close_midi(void);
int fd_write_midi(int *bytes_written, int count);
void midi_drain_due(void);
void midi_reset_state(void);
int midi_ring_full(void);
void midi_ring_push(uint32_t tick_due, int bank, int reg, int value);
void opl_silence_all(void);
void opl_write(int bank, int reg, int value);
