// ps2.c — PS/2 keyboard driver (32-bit protected mode).
//
// IRQ-driven.  The IDT 0x21 stub (ps2_irq1_handler, in the file-scope
// asm() block at the bottom) reads the raw scancode from port 0x60 and
// hands it to ps2_handle_scancode below, which tracks shift / ctrl /
// extended-prefix state, translates Set-1 make codes via two 59-byte
// tables, and pushes the result through ps2_putc into a 16-byte ring
// buffer drained by ps2_getc.
//
// Surface (matches the asm version):
//     ps2_getc — non-blocking, AL = ASCII char or 0 (ZF set if empty).
//     ps2_init — install IRQ 1 handler + unmask.  Once, before sti.
//
// ps2_install_irq lives in the asm block too because cc.py can't take
// the address of a label (idt_set_gate32 needs EAX = handler address);
// the two-line wrapper there hands the address to idt_set_gate32 and
// returns to C for the PIC unmask.
//
// Not supported (previously transparent through BIOS INT 16h):
//     Caps Lock — scancode 0x3A is in the table but toggle not tracked.
//     Alt       — scancode 0x38 is in the table but modifier not tracked.
//     F-keys    — codes 0x3B–0x44 are above the table boundary; discarded.
//     Extended keys beyond arrows (Home/End/PgUp/PgDn/Insert/Delete) —
//                 discarded.

#define KB_BUFFER_SIZE  16      // power of 2; mask = KB_BUFFER_SIZE - 1
#define PS2_DATA        0x60
#define PS2_IRQ1_MASK   0xFD    // ~(1 << 1): clear bit 1 to unmask IRQ 1
#define PS2_PIC1_DATA   0x21    // PIC1 IMR (PIC remapped to 0x20-0x27)

// BBKEY_* — positional keyboard codes pushed into the per-fd event
// ring (16-bit each, packed as ((pressed << 16) | code) into the
// 32-bit event slot).  Mirrors Linux's evdev KEY_* model: the
// kernel keeps the layout-agnostic code stable, modifier keys are
// first-class, and consumers can dispatch on the code without ever
// caring about ASCII / shift state.  KEEP IN SYNC with
// tools/libc/include/bbkeys.h (the userspace consumer header) —
// cc.py's preprocessor doesn't share an include path with the
// userspace clang toolchain so this header can't be a single
// source of truth.
#define BBKEY_RESERVED      0
#define BBKEY_LSHIFT        1
#define BBKEY_RSHIFT        2
#define BBKEY_LCTRL         3
#define BBKEY_RCTRL         4
#define BBKEY_LALT          5
#define BBKEY_RALT          6
#define BBKEY_CAPSLOCK      7
#define BBKEY_UP            8
#define BBKEY_DOWN          9
#define BBKEY_LEFT          10
#define BBKEY_RIGHT         11
#define BBKEY_ESC           12
#define BBKEY_BACKSPACE     13
#define BBKEY_TAB           14
#define BBKEY_ENTER         15
#define BBKEY_SPACE         16
#define BBKEY_A             17
#define BBKEY_B             18
#define BBKEY_C             19
#define BBKEY_D             20
#define BBKEY_E             21
#define BBKEY_F             22
#define BBKEY_G             23
#define BBKEY_H             24
#define BBKEY_I             25
#define BBKEY_J             26
#define BBKEY_K             27
#define BBKEY_L             28
#define BBKEY_M             29
#define BBKEY_N             30
#define BBKEY_O             31
#define BBKEY_P             32
#define BBKEY_Q             33
#define BBKEY_R             34
#define BBKEY_S             35
#define BBKEY_T             36
#define BBKEY_U             37
#define BBKEY_V             38
#define BBKEY_W             39
#define BBKEY_X             40
#define BBKEY_Y             41
#define BBKEY_Z             42
#define BBKEY_0             43
#define BBKEY_1             44
#define BBKEY_2             45
#define BBKEY_3             46
#define BBKEY_4             47
#define BBKEY_5             48
#define BBKEY_6             49
#define BBKEY_7             50
#define BBKEY_8             51
#define BBKEY_9             52
#define BBKEY_GRAVE         53
#define BBKEY_MINUS         54
#define BBKEY_EQUALS        55
#define BBKEY_LBRACKET      56
#define BBKEY_RBRACKET      57
#define BBKEY_BACKSLASH     58
#define BBKEY_SEMICOLON     59
#define BBKEY_APOSTROPHE    60
#define BBKEY_COMMA         61
#define BBKEY_PERIOD        62
#define BBKEY_SLASH         63
#define BBKEY_KP_STAR       64

// Ring buffer: single-producer (IRQ context, IF=0) /
// single-consumer (main loop) so head and tail don't need atomics.
// Cooked ASCII path (drained by fd_read_console / TRY_GETC); event
// path lives per-fd in the fd table — see ps2_broadcast_event below.
char ps2_buf[KB_BUFFER_SIZE];

// Modifier state — bytes flipped from the IRQ context as
// shift / ctrl press / release scancodes arrive.  Used by the
// cooked path to fold Shift into uppercase and Ctrl+letter into
// ^A..^Z.  The event path doesn't need them; BBKEY codes are
// positional (BBKEY_W is "the W slot" regardless of shift) with
// separate events for the modifier keys themselves.
uint8_t ps2_ctrl;
uint8_t ps2_extended;
uint8_t ps2_head;

// Set-1 scan code → ASCII tables (codes 0x00–0x3A only — F-keys /
// CapsLock land above 0x3A and get filtered out before the table is
// read).  Sorted by suffix: shift before unshift.
char ps2_map_shift[59] = {
    0,    0x1B, '!',  '@',  '#',  '$',  '%',  '^',  '&',  '*',
    '(',  ')',  '_',  '+',  0x08, 0x09, 'Q',  'W',  'E',  'R',
    'T',  'Y',  'U',  'I',  'O',  'P',  '{',  '}',  0x0D, 0,
    'A',  'S',  'D',  'F',  'G',  'H',  'J',  'K',  'L',  ':',
    '"',  '~',  0,    '|',  'Z',  'X',  'C',  'V',  'B',  'N',
    'M',  '<',  '>',  '?',  0,    '*',  0,    ' ',  0,
};

char ps2_map_unshift[59] = {
    0,    0x1B, '1',  '2',  '3',  '4',  '5',  '6',  '7',  '8',
    '9',  '0',  '-',  '=',  0x08, 0x09, 'q',  'w',  'e',  'r',
    't',  'y',  'u',  'i',  'o',  'p',  '[',  ']',  0x0D, 0,
    'a',  's',  'd',  'f',  'g',  'h',  'j',  'k',  'l',  ';',
    0x27, '`',  0,    '\\', 'z',  'x',  'c',  'v',  'b',  'n',
    'm',  ',',  '.',  '/',  0,    '*',  0,    ' ',  0,
};

// Set-1 scancode -> BBKEY positional code.  Mirrors ps2_map_unshift
// in length and indexing so the same `code` index drops into both
// arrays, but produces a layout-independent identifier for the
// event ring (ASCII/shift state lives in the cooked path only).
// Modifier scancodes (Shift/Ctrl) and CapsLock are first-class
// here, unlike in ps2_map_unshift where they slot 0 to be skipped.
uint8_t ps2_scancode_to_bbkey[59] = {
    /* 0x00 */ 0,
    /* 0x01 */ BBKEY_ESC,
    /* 0x02 */ BBKEY_1,
    /* 0x03 */ BBKEY_2,
    /* 0x04 */ BBKEY_3,
    /* 0x05 */ BBKEY_4,
    /* 0x06 */ BBKEY_5,
    /* 0x07 */ BBKEY_6,
    /* 0x08 */ BBKEY_7,
    /* 0x09 */ BBKEY_8,
    /* 0x0A */ BBKEY_9,
    /* 0x0B */ BBKEY_0,
    /* 0x0C */ BBKEY_MINUS,
    /* 0x0D */ BBKEY_EQUALS,
    /* 0x0E */ BBKEY_BACKSPACE,
    /* 0x0F */ BBKEY_TAB,
    /* 0x10 */ BBKEY_Q,
    /* 0x11 */ BBKEY_W,
    /* 0x12 */ BBKEY_E,
    /* 0x13 */ BBKEY_R,
    /* 0x14 */ BBKEY_T,
    /* 0x15 */ BBKEY_Y,
    /* 0x16 */ BBKEY_U,
    /* 0x17 */ BBKEY_I,
    /* 0x18 */ BBKEY_O,
    /* 0x19 */ BBKEY_P,
    /* 0x1A */ BBKEY_LBRACKET,
    /* 0x1B */ BBKEY_RBRACKET,
    /* 0x1C */ BBKEY_ENTER,
    /* 0x1D */ BBKEY_LCTRL,
    /* 0x1E */ BBKEY_A,
    /* 0x1F */ BBKEY_S,
    /* 0x20 */ BBKEY_D,
    /* 0x21 */ BBKEY_F,
    /* 0x22 */ BBKEY_G,
    /* 0x23 */ BBKEY_H,
    /* 0x24 */ BBKEY_J,
    /* 0x25 */ BBKEY_K,
    /* 0x26 */ BBKEY_L,
    /* 0x27 */ BBKEY_SEMICOLON,
    /* 0x28 */ BBKEY_APOSTROPHE,
    /* 0x29 */ BBKEY_GRAVE,
    /* 0x2A */ BBKEY_LSHIFT,
    /* 0x2B */ BBKEY_BACKSLASH,
    /* 0x2C */ BBKEY_Z,
    /* 0x2D */ BBKEY_X,
    /* 0x2E */ BBKEY_C,
    /* 0x2F */ BBKEY_V,
    /* 0x30 */ BBKEY_B,
    /* 0x31 */ BBKEY_N,
    /* 0x32 */ BBKEY_M,
    /* 0x33 */ BBKEY_COMMA,
    /* 0x34 */ BBKEY_PERIOD,
    /* 0x35 */ BBKEY_SLASH,
    /* 0x36 */ BBKEY_RSHIFT,
    /* 0x37 */ BBKEY_KP_STAR,
    /* 0x38 */ BBKEY_LALT,
    /* 0x39 */ BBKEY_SPACE,
    /* 0x3A */ BBKEY_CAPSLOCK,
};

uint8_t ps2_shift;
uint8_t ps2_tail;

// Forward declarations for callees that come later alphabetically.
// ps2_install_irq and ps2_broadcast_event are asm shims in the
// trailing asm() block.  ps2_putc is a C body further down — it
// sorts after ps2_handle_scancode (its only caller).
void ps2_broadcast_event(int entry __attribute__((in_register("ax"))));
void ps2_install_irq();
void ps2_putc(char byte __attribute__((in_register("ax"))));

// ps2_drain: discard buffered ASCII and reset modifier state.  Called
// from program_enter so a freshly-loaded program doesn't see ps2_buf
// bytes the previous program had buffered (e.g. gameplay keys still
// in the ring when Doom exited — Doom drains TRY_GET_EVENT but not
// the cooked ASCII ring, so up to KB_BUFFER_SIZE bytes can sit
// stale).  Modifier latches also get cleared in case the matching
// break code was eaten by the program teardown.
//
// The per-fd event queues don't need draining here — fd_init zeros
// the entire fd table on every program load, which clears head /
// tail / buffer for every console fd in one shot.  Called with IF=0
// (kernel context, between sti gates) so the IRQ can't simultaneously
// re-fill ps2_buf mid-reset.
void ps2_drain() {
    ps2_head = 0;
    ps2_tail = 0;
    ps2_shift = 0;
    ps2_ctrl = 0;
    ps2_extended = 0;
}

// ps2_getc: pull one ASCII byte from the ring; return 0 if empty.
// Non-blocking; the asm-version contract leaves ZF set on empty
// (AL=0), which the asm caller in `fs/fd/console.asm` checks via
// ``test al, al`` — char return matches that contract directly.
char ps2_getc() {
    uint8_t head;
    char byte;
    if (ps2_head == ps2_tail) { return '\0'; }
    head = ps2_head;
    byte = ps2_buf[head];
    ps2_head = (head + 1) & (KB_BUFFER_SIZE - 1);
    return byte;
}

// ps2_handle_scancode: process one raw byte from port 0x60.  Two
// parallel output channels:
//
//   1. Cooked: ASCII (with shift / ctrl folding) into ps2_buf, plus
//      ANSI CSI sequences (ESC [ A/B/C/D) for arrow keys.  Drained
//      by fd_read_console / TRY_GETC.  Press-only — releases never
//      surface in the cooked stream (same as Linux ttys).
//
//   2. Event: per-fd ring of (pressed << 16) | BBKEY_* slots, fed
//      via ps2_broadcast_event.  Press AND release for every key,
//      modifier keys included as first-class.  Drained by
//      TRY_GET_EVENT.  Positional codes — BBKEY_W is "the W slot"
//      regardless of shift / layout, so consumers like Doom can
//      dispatch on physical key without reasoning about ASCII.
//
// Called from the IRQ stub with the scancode in AL.
void ps2_handle_scancode(uint8_t scancode __attribute__((in_register("ax")))) {
    uint8_t code;
    uint8_t pressed;
    int bbkey;
    char ascii;
    char upper;

    // Extended-key prefix: stash the flag and wait for the next byte
    // (arrow keys, RCtrl, RAlt, etc. all arrive as 0xE0 + scancode).
    if (scancode == 0xE0) { ps2_extended = 1; return; }

    if ((scancode & 0x80) == 0) {
        pressed = 1;
    } else {
        pressed = 0;
    }
    code = scancode & 0x7F;
    bbkey = 0;

    if (ps2_extended != 0) {
        ps2_extended = 0;
        // Map the recognised extended scancodes to BBKEY codes.
        // Discard everything else (Home, End, PgUp, PgDn, Ins, Del).
        if (code == 0x48) { bbkey = BBKEY_UP; }
        else if (code == 0x50) { bbkey = BBKEY_DOWN; }
        else if (code == 0x4D) { bbkey = BBKEY_RIGHT; }
        else if (code == 0x4B) { bbkey = BBKEY_LEFT; }
        else if (code == 0x1D) { bbkey = BBKEY_RCTRL; ps2_ctrl = pressed; }
        else if (code == 0x38) { bbkey = BBKEY_RALT; }
        if (bbkey == 0) { return; }
        // Cooked path: emit ANSI CSI sequence on press only (arrows
        // only — RCtrl / RAlt aren't cooked-visible, same as on Linux).
        if (pressed != 0) {
            if (bbkey == BBKEY_UP)         { ps2_putc('\x1B'); ps2_putc('['); ps2_putc('A'); }
            else if (bbkey == BBKEY_DOWN)  { ps2_putc('\x1B'); ps2_putc('['); ps2_putc('B'); }
            else if (bbkey == BBKEY_RIGHT) { ps2_putc('\x1B'); ps2_putc('['); ps2_putc('C'); }
            else if (bbkey == BBKEY_LEFT)  { ps2_putc('\x1B'); ps2_putc('['); ps2_putc('D'); }
        }
        ps2_broadcast_event((pressed << 16) | bbkey);
        return;
    }

    // Regular key.  F-keys (0x3B–0x44) sit above the table boundary;
    // discard rather than read past the array.
    if (code >= 0x3B) { return; }

    bbkey = ps2_scancode_to_bbkey[code];
    if (bbkey == 0) { return; }

    // Modifier latch updates happen for both press and release so
    // the cooked path's shift / ctrl folding stays in sync with what
    // the user is holding.
    if (bbkey == BBKEY_LSHIFT || bbkey == BBKEY_RSHIFT) {
        ps2_shift = pressed;
    } else if (bbkey == BBKEY_LCTRL) {
        ps2_ctrl = pressed;
    }

    // Cooked path: ASCII translation on press only.  Modifier keys
    // produce ascii == 0 in the keymap tables and so contribute
    // nothing to the cooked stream.
    if (pressed != 0) {
        if (ps2_shift != 0) {
            ascii = ps2_map_shift[code];
        } else {
            ascii = ps2_map_unshift[code];
        }
        if (ps2_ctrl != 0 && ascii != '\0') {
            // Ctrl + letter -> ^A..^Z control code.  Force-uppercase
            // first so ctrl works regardless of shift state.
            upper = ascii & 0x5F;
            if (upper >= 'A' && upper <= 'Z') {
                ascii = upper - ('A' - 1);
            }
        }
        if (ascii != '\0') {
            ps2_putc(ascii);
        }
    }

    // Event path: every press and release goes through, including
    // modifier keys themselves.  Cooked-path consumers see only the
    // folded ASCII; event-path consumers see the raw key activity.
    ps2_broadcast_event((pressed << 16) | bbkey);
}

// ps2_init: install the IRQ 1 handler and unmask it on the master PIC.
// Call once from entry.asm before sti.
void ps2_init() {
    uint8_t mask;
    ps2_install_irq();
    mask = kernel_inb(PS2_PIC1_DATA);
    kernel_outb(PS2_PIC1_DATA, mask & PS2_IRQ1_MASK);
}

// ps2_install_irq: register ps2_irq1_handler at IDT vector 0x21.
//     idt_set_gate32 takes EAX = handler address, BL = vector — cc.py
//     has no syntax for taking the address of a label, so this two-
//     instruction wrapper has to be asm.
//
// ps2_irq1_handler: IRQ 1 stub.  Reads the raw scancode from port
// 0x60, calls into ps2_handle_scancode (a real C function above —
// NASM resolves the label across the asm() block), EOIs the master
// PIC, and iretds.  Must end with iretd, not ret.
//
// ps2_broadcast_event: walk fd_table; for each FD_TYPE_CONSOLE
// entry whose flags clear O_WRONLY (i.e. readable — events go to
// the cooked-input side, mirroring Linux's evdev model), push EAX
// into that fd's inline event ring at FD_OFFSET_EVENT_BUF, indexed
// by FD_OFFSET_EVENT_TAIL.  Per-queue full → silently drop.  Saves
// every register so callers from ps2_handle_scancode (a cc.py C
// body) don't see scratch clobbers.
asm("
ps2_install_irq:
        push eax
        push ebx
        mov eax, ps2_irq1_handler
        mov bl, 0x21
        call idt_set_gate32
        pop ebx
        pop eax
        ret

ps2_irq1_handler:
        ;; pushad envelope: ps2_handle_scancode is a C body that uses
        ;; ECX/EDX/ESI as scratch (cc.py-emitted frame setup).  IRQ 1
        ;; can fire at any user-mode instruction boundary, so anything
        ;; the C body touches has to be saved or the user program sees
        ;; corrupted registers.  pushad covers the whole integer file
        ;; cheaply and survives future C-body changes that add new
        ;; scratch.
        pushad
        in al, 0x60
        call ps2_handle_scancode
        mov al, 0x20
        out 0x20, al
        popad
        iretd

ps2_broadcast_event:
        ;; EAX = (pressed << 8) | byte to broadcast.  Saves EBX/ECX/
        ;; EDX/ESI/EDI even though the IRQ stub already pushaded —
        ;; ps2_handle_scancode (a cc.py C body) calls us between its
        ;; scratch reloads, so any clobber would surface as wrong
        ;; modifier-state writes after we return.
        push ebx
        push ecx
        push edx
        push esi
        push edi
        mov edi, eax                            ; EDI = event (preserved across loop)
        mov esi, fd_table                       ; ESI walks fd entries
        xor ecx, ecx                            ; ECX = fd index
.ps2_broadcast_event_loop:
        cmp ecx, FD_MAX
        jae .ps2_broadcast_event_done
        cmp byte [esi + FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        jne .ps2_broadcast_event_next
        test byte [esi + FD_OFFSET_FLAGS], O_WRONLY
        jnz .ps2_broadcast_event_next           ; write-only console fd — skip
        movzx ebx, byte [esi + FD_OFFSET_EVENT_TAIL]
        mov dl, bl
        inc dl
        and dl, FD_EVENT_QUEUE_LEN - 1
        cmp dl, [esi + FD_OFFSET_EVENT_HEAD]
        je .ps2_broadcast_event_next            ; queue full — drop
        mov [esi + FD_OFFSET_EVENT_BUF + ebx*4], edi
        mov [esi + FD_OFFSET_EVENT_TAIL], dl
.ps2_broadcast_event_next:
        add esi, FD_ENTRY_SIZE
        inc ecx
        jmp .ps2_broadcast_event_loop
.ps2_broadcast_event_done:
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        ret
");

// ps2_putc: push one byte into the ring; drop silently when full.
// Called only from the IRQ handler path (ps2_handle_scancode) where
// IF=0, so head/tail concurrency is one-sided.
void ps2_putc(char byte __attribute__((in_register("ax")))) {
    uint8_t tail;
    uint8_t next;
    tail = ps2_tail;
    next = (tail + 1) & (KB_BUFFER_SIZE - 1);
    if (next == ps2_head) { return; }
    ps2_buf[tail] = byte;
    ps2_tail = next;
}

// ps2_broadcast_event: push one (pressed << 8 | ascii) entry into
// every readable FD_TYPE_CONSOLE fd's per-fd event ring.  Drops
// silently on per-queue overflow (same IRQ-only contract as ps2_putc).
//
// Implemented as inline asm in the trailing asm() block — the loop
// touches fd_table entries by FD_OFFSET_* offset and stores 32-bit
// event slots into the inline event_buf, which cc.py can't easily
// express against the byte-array struct field.  The asm version
// also avoids tripping cc.py's "extern array of struct" path
// (fd_table is owned by fs/fd.c).
void ps2_broadcast_event(int entry __attribute__((in_register("ax"))));
