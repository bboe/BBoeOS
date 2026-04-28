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

// Ring buffer: single-producer (IRQ context, IF=0) /
// single-consumer (main loop) so head and tail don't need atomics.
char ps2_buf[KB_BUFFER_SIZE];

// Modifier state — bytes flipped from the IRQ context as
// shift / ctrl press / release scancodes arrive.
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

uint8_t ps2_shift;
uint8_t ps2_tail;

// Forward declarations for callees that come later alphabetically.
// ps2_install_irq is the asm-shim below (cc.py can't take the address
// of a label).  ps2_putc is reached from ps2_handle_scancode which
// sorts before ps2_putc.
void ps2_install_irq();
void ps2_putc(char byte __attribute__((in_register("ax"))));

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

// ps2_handle_scancode: process one raw byte from port 0x60.  Updates
// shift / ctrl / extended modifier state and pushes translated bytes
// (ASCII for regular keys, ANSI CSI sequences for arrow keys) into the
// ring buffer.  Called from the IRQ stub with the scancode in AL.
void ps2_handle_scancode(uint8_t scancode __attribute__((in_register("ax")))) {
    uint8_t code;
    char ascii;
    char upper;

    // Modifier release codes carry bit 7 set on top of the press code.
    if (scancode == 0xAA) { ps2_shift = 0; ps2_extended = 0; return; }
    if (scancode == 0xB6) { ps2_shift = 0; ps2_extended = 0; return; }
    if (scancode == 0x9D) { ps2_ctrl = 0;  ps2_extended = 0; return; }

    // All other break codes — discard, but clear the extended flag
    // so a stale 0xE0 doesn't corrupt the next press.
    if ((scancode & 0x80) != 0) { ps2_extended = 0; return; }

    // Extended-key prefix: keep the flag set so the next make code
    // dispatches through the arrow-key branch below.
    if (scancode == 0xE0) { ps2_extended = 1; return; }

    // Modifier press codes.
    if (scancode == 0x2A) { ps2_shift = 1; ps2_extended = 0; return; }
    if (scancode == 0x36) { ps2_shift = 1; ps2_extended = 0; return; }
    if (scancode == 0x1D) { ps2_ctrl = 1;  ps2_extended = 0; return; }

    if (ps2_extended != 0) {
        // Arrow keys: emit ESC '[' A/B/C/D as three buffer entries.
        // Other 0xE0-prefixed keys (Home / End / PgUp / PgDn / Ins /
        // Del) are discarded.
        if (scancode == 0x48) {
            ps2_putc('\x1B'); ps2_putc('['); ps2_putc('A');
        } else if (scancode == 0x50) {
            ps2_putc('\x1B'); ps2_putc('['); ps2_putc('B');
        } else if (scancode == 0x4D) {
            ps2_putc('\x1B'); ps2_putc('['); ps2_putc('C');
        } else if (scancode == 0x4B) {
            ps2_putc('\x1B'); ps2_putc('['); ps2_putc('D');
        }
        ps2_extended = 0;
        return;
    }

    // Regular key.  F-keys (0x3B–0x44) and CapsLock (0x3A) sit above
    // the table boundary — discard rather than read past the array.
    if (scancode >= 0x3B) { ps2_extended = 0; return; }

    code = scancode;
    if (ps2_shift != 0) {
        ascii = ps2_map_shift[code];
    } else {
        ascii = ps2_map_unshift[code];
    }
    if (ascii == '\0') { ps2_extended = 0; return; }

    // Ctrl + letter → control code (^A=1 … ^Z=26).  Force-uppercase
    // first so ctrl works regardless of shift state.
    if (ps2_ctrl != 0) {
        upper = ascii & 0x5F;
        if (upper >= 'A' && upper <= 'Z') {
            ascii = upper - ('A' - 1);
        }
    }
    ps2_putc(ascii);
    ps2_extended = 0;
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
