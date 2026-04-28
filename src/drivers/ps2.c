// ps2.c — native PS/2 keyboard driver.
//
// Replaces INT 16h AH=00h and AH=01h for fd.c's console-read path by
// polling the 8042 directly: port 0x60 is the data register, port 0x64
// carries the status byte (bit 0 = output-buffer full).
//
// Translates Set 1 scan codes to ASCII via a small unshifted / shifted
// keymap pair.  Tracks shift and ctrl state across make/break pairs, and
// decodes the 0xE0 prefix just for the cursor-pad arrows.  A one-slot
// buffer holds a decoded key so ps2_check can peek without consuming.
//
// Surface (both decoded keys carry BIOS-compatible AL = ASCII / AH = scan code):
//     ps2_init        - mask IRQ 1 at the master PIC so the BIOS IRQ
//                       handler stops draining port 0x60 behind us.
//                       Zeros the driver state.  Call once, early.
//     ps2_check       - carry_return: CF clear if a decoded key is ready,
//                       CF set otherwise.  Non-blocking; does not consume
//                       the buffered key.  (The asm version returned ZF;
//                       fd.c's ps2_has_key wrapper used to do the
//                       ZF→CF translation — now redundant, removed.)
//     ps2_read        - blocks until a key is ready, returns AL = ASCII
//                       (0 for extended arrows) and AH = scan code packed
//                       into the int return so callers can split into
//                       ascii = packed & 0xFF and scan = (packed >> 8) & 0xFF.

// Names are PS2_* prefixed (vs the asm version's PIC1_DATA / PIC_IRQ1_MASK)
// to avoid clashing with the ``equ`` definitions of the same names in
// drivers/rtc.asm — cc.py's #define expands as a NASM macro, so any
// ``PIC1_DATA equ 21h`` later in the same translation unit becomes
// ``0x21 equ 21h`` after macro substitution and refuses to assemble.
#define PS2_DATA               0x60
#define PS2_STATUS             0x64
#define PS2_STATUS_OUTPUT_FULL 0x01
#define PS2_PIC1_DATA          0x21
#define PS2_PIC_IRQ1_MASK      0x02

uint8_t ps2_buffered;
uint8_t ps2_buffered_al;
uint8_t ps2_buffered_ah;
uint8_t ps2_extended;
uint8_t ps2_shift;
uint8_t ps2_ctrl;

// Set-1 scan code → ASCII.  Index 0 is a dummy; scan codes beyond
// 0x3A are function keys / CapsLock and are rejected before the table
// is consulted.
uint8_t ps2_map_unshift[59] = {
    0,    0x1B,  '1',  '2',  '3',  '4',  '5',  '6',  '7',  '8',
    '9',  '0',   '-',  '=',  0x08, 0x09, 'q',  'w',  'e',  'r',
    't',  'y',   'u',  'i',  'o',  'p',  '[',  ']',  0x0D, 0,
    'a',  's',   'd',  'f',  'g',  'h',  'j',  'k',  'l',  ';',
    0x27, '`',   0,    '\\', 'z',  'x',  'c',  'v',  'b',  'n',
    'm',  ',',   '.',  '/',  0,    '*',  0,    ' ',  0,
};

uint8_t ps2_map_shift[59] = {
    0,    0x1B,  '!',  '@',  '#',  '$',  '%',  '^',  '&',  '*',
    '(',  ')',   '_',  '+',  0x08, 0x09, 'Q',  'W',  'E',  'R',
    'T',  'Y',   'U',  'I',  'O',  'P',  '{',  '}',  0x0D, 0,
    'A',  'S',   'D',  'F',  'G',  'H',  'J',  'K',  'L',  ':',
    '"',  '~',   0,    '|',  'Z',  'X',  'C',  'V',  'B',  'N',
    'M',  '<',   '>',  '?',  0,    '*',  0,    ' ',  0,
};

void ps2_init() {
    int mask;
    mask = kernel_inb(PS2_PIC1_DATA);
    kernel_outb(PS2_PIC1_DATA, mask | PS2_PIC_IRQ1_MASK);
    ps2_buffered = 0;
    ps2_buffered_al = 0;
    ps2_buffered_ah = 0;
    ps2_extended = 0;
    ps2_shift = 0;
    ps2_ctrl = 0;
}

// ps2_read_scancode: drain one byte from the 8042 if available.
// Returns CF clear on success (with AX = scancode in low byte), CF set
// when the output buffer is empty.  Internal helper used by ps2_service.
__attribute__((carry_return))
int ps2_read_scancode(int *scancode __attribute__((out_register("ax")))) {
    if ((kernel_inb(PS2_STATUS) & PS2_STATUS_OUTPUT_FULL) == 0) {
        return 0;
    }
    *scancode = kernel_inb(PS2_DATA);
    return 1;
}

// ps2_service: consume any pending scan codes from the 8042, updating
// modifier state and populating the decoded-key buffer.  Returns when
// either the buffer is filled or the hardware queue is empty.
void ps2_service() {
    int scancode;
    int code;
    int ascii;
    int upper;
    while (1) {
        if (ps2_buffered != 0) { return; }
        if (!ps2_read_scancode(&scancode)) { return; }
        if (scancode == 0xE0) {
            ps2_extended = 1;
            continue;
        }
        code = scancode & 0x7F;
        if ((scancode & 0x80) != 0) {
            // Break (key release) — only modifier releases matter.
            if (code == 0x2A) { ps2_shift = 0; }
            if (code == 0x36) { ps2_shift = 0; }
            if (code == 0x1D) { ps2_ctrl = 0; }
            ps2_extended = 0;
            continue;
        }
        // Make (key press).
        if (code == 0x2A) {
            ps2_shift = 1;
            ps2_extended = 0;
            continue;
        }
        if (code == 0x36) {
            ps2_shift = 1;
            ps2_extended = 0;
            continue;
        }
        if (code == 0x1D) {
            ps2_ctrl = 1;
            ps2_extended = 0;
            continue;
        }
        if (ps2_extended != 0) {
            // Extended prefix set — only cursor-pad arrows produce
            // output (matches BIOS INT 16h AH=00 semantics).
            if (code == 0x48 || code == 0x50 || code == 0x4D || code == 0x4B) {
                ps2_buffered_al = 0;
                ps2_buffered_ah = code;
                ps2_buffered = 1;
            }
            ps2_extended = 0;
            continue;
        }
        // Regular key.
        ps2_extended = 0;
        if (code >= 0x3B) { continue; }
        if (ps2_shift != 0) {
            ascii = ps2_map_shift[code];
        } else {
            ascii = ps2_map_unshift[code];
        }
        if (ascii == 0) { continue; }
        if (ps2_ctrl != 0) {
            // Ctrl+letter → control code (1..26).  Non-letters pass through.
            upper = ascii & 0x5F;
            if (upper >= 'A') {
                if (upper <= 'Z') {
                    ascii = upper - ('A' - 1);
                }
            }
        }
        ps2_buffered_al = ascii;
        ps2_buffered_ah = 0;
        ps2_buffered = 1;
    }
}

// ps2_check: CF clear if a decoded key is ready, CF set otherwise.
// Drains pending scan codes into the buffer first.  Non-blocking.
__attribute__((carry_return))
int ps2_check() {
    ps2_service();
    if (ps2_buffered != 0) { return 1; }
    return 0;
}

// ps2_read: block until a decoded key is ready; returns AL=ASCII and
// AH=scan-code packed into the int return — callers split via
// ``ascii = packed & 0xFF`` and ``scan = (packed >> 8) & 0xFF``.
int ps2_read() {
    int packed;
    while (ps2_buffered == 0) {
        ps2_service();
    }
    packed = (ps2_buffered_ah << 8) | ps2_buffered_al;
    ps2_buffered = 0;
    return packed;
}
