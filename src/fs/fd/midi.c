// fd/midi.c — read/write/ioctl/close handlers for FD_TYPE_MIDI
// (/dev/midi).  Companion to drivers/opl3.c.
//
// Wire format: each command is 6 bytes —
//     uint16_t delay     (PIT ticks since previous command in this stream)
//     uint8_t  bank      (0 → 0x388/0x389, 1 → 0x38A/0x38B; others ignored)
//     uint8_t  reg       (OPL register 0x00..0xFF)
//     uint8_t  value     (register value)
//     uint8_t  reserved  (must be 0)
//
// Kernel keeps a 256-slot ring (255 effective capacity) of
// (tick_due, bank, reg, value) tuples.  fd_write_midi parses the
// userland buffer, advances the per-fd virtual clock by `delay`, and
// appends a tuple per well-formed command.  The IRQ 0 ISR
// (arch/x86/entry.asm) calls midi_drain_due to walk events whose
// tick_due ≤ system_ticks and emit them via opl_write.
//
// Single-instance for v1: only one /dev/midi may be open at a time.
// fd_open's /dev/midi branch enforces this; the ring is global rather
// than per-fd.

extern uint8_t *fd_write_buffer;
extern uint8_t opl3_present;
extern uint32_t system_ticks;

// drivers/opl3.c
void opl_silence_all();
void opl_write(int bank, int reg, int value);

#define MIDI_COMMAND_BYTES  6
#define MIDI_DRAIN_PER_TICK 16   // bound on per-ISR work
#define MIDI_RING_SIZE      256

struct midi_event {
    uint8_t  _pad;
    uint8_t  bank;
    uint8_t  reg;
    uint32_t tick_due;
    uint8_t  value;
};

uint8_t  midi_head;          // next slot to consume
asm("midi_head equ _g_midi_head");
struct midi_event midi_ring[MIDI_RING_SIZE];
asm("midi_ring equ _g_midi_ring");
uint8_t  midi_tail;          // next slot to produce
asm("midi_tail equ _g_midi_tail");
uint32_t midi_virtual_clock; // PIT ticks; per-fd but stored globally because single-instance
asm("midi_virtual_clock equ _g_midi_virtual_clock");

// Forward declarations for the helpers fd_write_midi calls.  Functions
// are listed alphabetically below; midi_ring_full and midi_ring_push
// land after fd_write_midi in source order so they need a forward
// signature here for cc.py to resolve the call sites.
int midi_ring_full();
void midi_ring_push(uint32_t tick_due, int bank, int reg, int value);

// fd_close_midi: per-/dev/midi-close hook.  Drop queued events and
// silence the chip so a Doom crash can't leave 18 stuck FM voices.
void fd_close_midi() {
    midi_head = 0;
    midi_tail = 0;
    opl_silence_all();
}

// fd_ioctl_midi: SYS_IO_IOCTL backend.  Same shape as fd_ioctl_audio:
// AL = command, ESI = fd entry pointer.
//
//   MIDI_IOCTL_DRAIN (0x00) — block via sti/hlt until head == tail
//                              (i.e. every queued event has been
//                              emitted to the chip); AX = 0, CF clear
//   MIDI_IOCTL_FLUSH (0x01) — drop queued events + KEY_OFF, CF clear
//   MIDI_IOCTL_QUERY (0x02) — AX = opl3_present, CF clear
//   anything else           — CF set
void fd_ioctl_midi();

asm("fd_ioctl_midi:\n"
    "        cmp al, MIDI_IOCTL_DRAIN\n"
    "        je .fd_ioctl_midi_drain\n"
    "        cmp al, MIDI_IOCTL_FLUSH\n"
    "        je .fd_ioctl_midi_flush\n"
    "        cmp al, MIDI_IOCTL_QUERY\n"
    "        je .fd_ioctl_midi_query\n"
    "        stc\n"
    "        ret\n"
    ".fd_ioctl_midi_drain:\n"
    // Block until midi_drain_due (called from the IRQ 0 ISR) has
    // emitted every queued event.  cli/sti bracket each head/tail
    // read so we never race with the ISR's increment of midi_head.
    // sti+hlt as a single sequence is the standard low-power-wait
    // pattern — the CPU wakes on the next IRQ, the ISR drains zero
    // or more due events, and we re-check on the loop body.
    ".fd_ioctl_midi_drain_wait:\n"
    "        cli\n"
    "        mov al, [_g_midi_head]\n"
    "        cmp al, [_g_midi_tail]\n"
    "        je .fd_ioctl_midi_drain_done\n"
    "        sti\n"
    "        hlt\n"
    "        jmp .fd_ioctl_midi_drain_wait\n"
    ".fd_ioctl_midi_drain_done:\n"
    "        sti\n"
    "        xor eax, eax\n"
    "        clc\n"
    "        ret\n"
    ".fd_ioctl_midi_flush:\n"
    "        push ecx\n"
    "        push edx\n"
    "        mov byte [_g_midi_head], 0\n"
    "        mov byte [_g_midi_tail], 0\n"
    "        call opl_silence_all\n"
    "        pop edx\n"
    "        pop ecx\n"
    "        xor eax, eax\n"
    "        clc\n"
    "        ret\n"
    ".fd_ioctl_midi_query:\n"
    "        movzx eax, byte [_g_opl3_present]\n"
    "        clc\n"
    "        ret\n");

// fd_write_midi: parse 6-byte commands from fd_write_buffer.  For each
// well-formed command, advance the virtual clock by `delay`; if the
// ring has space and bank ∈ {0,1}, enqueue the tuple.  If the ring is
// full, return early — the bytes already consumed count toward the
// returned bytes_written, the rest is the userland's problem on next
// write.  AX = bytes consumed (multiple of 6), CF clear.
__attribute__((carry_return))
int fd_write_midi(int *bytes_written __attribute__((out_register("ax"))),
                  int count __attribute__((in_register("ecx")))) {
    int bank;
    int consumed;
    int delay;
    int reg;
    uint32_t tick_due;
    int value;
    consumed = 0;
    while ((count - consumed) >= MIDI_COMMAND_BYTES) {
        if (midi_ring_full()) {
            break;
        }
        delay = fd_write_buffer[consumed] | (fd_write_buffer[consumed + 1] << 8);
        bank = fd_write_buffer[consumed + 2];
        reg = fd_write_buffer[consumed + 3];
        value = fd_write_buffer[consumed + 4];
        midi_virtual_clock = midi_virtual_clock + delay;
        if (bank <= 1) {
            tick_due = midi_virtual_clock;
            midi_ring_push(tick_due, bank, reg, value);
        }
        consumed = consumed + MIDI_COMMAND_BYTES;
    }
    *bytes_written = consumed;
    return 1;
}

// midi_drain_due: IRQ 0 ISR helper.  Walks events whose tick_due ≤
// system_ticks; emits each via opl_write; advances midi_head.  Bounded
// to MIDI_DRAIN_PER_TICK iterations to keep ISR latency O(1).
void midi_drain_due() {
    int bank;
    int drained;
    int reg;
    uint32_t tick_due;
    int value;
    drained = 0;
    while (drained < MIDI_DRAIN_PER_TICK && midi_head != midi_tail) {
        tick_due = midi_ring[midi_head].tick_due;
        if (tick_due > system_ticks) {
            return;
        }
        bank = midi_ring[midi_head].bank;
        reg = midi_ring[midi_head].reg;
        value = midi_ring[midi_head].value;
        opl_write(bank, reg, value);
        midi_head = (midi_head + 1) & 0xFF;
        drained = drained + 1;
    }
}

// midi_reset_state: called from fd_open_midi.  Drops queued events,
// anchors the virtual clock to the current system_ticks (so per-
// command delays land in the future relative to wall time, not at
// tick 0 which is far in the past once the kernel has been running),
// silences the chip.  Anchoring is load-bearing: midi_drain_due
// emits events whose tick_due ≤ system_ticks, so a clock that starts
// at 0 makes every command (regardless of its programmed delay)
// drain on the very next ISR firing — the queue's timing semantics
// only work when the virtual clock and system_ticks share an origin.
void midi_reset_state() {
    midi_head = 0;
    midi_tail = 0;
    midi_virtual_clock = system_ticks;
    opl_silence_all();
}

// midi_ring_full: returns 1 when the ring has 255 entries queued.
int midi_ring_full() {
    uint8_t next;
    next = (midi_tail + 1) & 0xFF;
    return next == midi_head;
}

// midi_ring_push: enqueue (tick_due, bank, reg, value).  Caller has
// already verified the ring is not full.
void midi_ring_push(uint32_t tick_due, int bank, int reg, int value) {
    midi_ring[midi_tail].tick_due = tick_due;
    midi_ring[midi_tail].bank = bank;
    midi_ring[midi_tail].reg = reg;
    midi_ring[midi_tail].value = value;
    midi_tail = (midi_tail + 1) & 0xFF;
}
