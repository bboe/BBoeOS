// console.c — full ANSI escape sequence parser.
//
// put_character: unified output to screen (with ANSI parsing) and serial.
//
// Same register-level ABI as the original drivers/console.asm.  All state
// lives in file-scope
// globals; ansi_fg keeps its bare asm symbol name so drivers/vga.asm
// (the one outside reader of [ansi_fg]) links unchanged.
//
// Supported CSI sequences:
//   ESC [ n A          cursor up
//   ESC [ n C          cursor forward
//   ESC [ n D          cursor back
//   ESC [ r ; c H      cursor position (1-indexed)
//   ESC [ 0 m          reset foreground to 7, background to 0
//   ESC [ 38;5;N m     256-color foreground (stored in ansi_fg)
//   ESC [ 48;5;N m     256-color background (palette via vga_set_bg)
//   ESC [ N @          write char code N at cursor, no advance or scroll
//
// Constants are inlined as bare integers (no #define) to avoid
// clashing with the asm namespace shared with drivers/vga.asm
// (cc.py emits #define as ``%define`` which would collide with
// vga.asm's ``VGA_COLS equ 80`` and friends).

uint8_t ansi_active_param;
uint8_t ansi_fg = 7;
int ansi_params[3];
uint8_t ansi_state;

// drivers/vga.asm reads the foreground attribute as ``[ansi_fg]``.
// cc.py emits the C-side storage with a ``_g_`` prefix; alias the
// bare name so the asm consumer doesn't need to know about that.
asm("ansi_fg equ _g_ansi_fg");

// Forward declarations for cross-file callees.  cc.py needs the
// signature (in_register / preserve_register pinning) at every call
// site, so they all live here at the top of the file.

// serial_character (drivers/serial.c): AL = char; preserves AX/DX.
void serial_character(char byte __attribute__((in_register("ax"))));

// vga_get_cursor: DH=row, DL=col packed in DX.  void function with an
// out_register parameter so cc.py emits the call then captures DX
// into the caller's int variable.
void vga_get_cursor(int *dx_out __attribute__((out_register("dx"))));

// vga_set_bg: AL = color.
void vga_set_bg(uint8_t color __attribute__((in_register("ax"))));

// vga_set_cursor: DH=row, DL=col (packed in DX).
void vga_set_cursor(int row_col __attribute__((in_register("dx"))));

// vga_teletype: AL = char.  Preserves all registers.
void vga_teletype(char byte __attribute__((in_register("ax"))));

// vga_write_attribute: AL = char, BL = attribute byte.
void vga_write_attribute(char byte __attribute__((in_register("ax"))),
                         uint8_t attr __attribute__((in_register("bx"))));

// preserve_register uses the E-register names so cc.py emits 32-bit
// push/pop (NOTES.md landmine #2).  ESI is also preserved because
// the ANSI CSI digit-accumulation path uses it as scratch and asm
// callers in entry.asm don't expect it to be clobbered.
void put_character(char byte __attribute__((in_register("ax"))))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
    __attribute__((preserve_register("esi")))
{
    int p1;
    int p2;
    int p3;
    int dx_packed;
    int row;
    int col;
    int linear;

    if (byte == '\n') {
        // Convert \n to \r\n: emit CR to both serial and screen first,
        // then fall through and the state machine handles the LF byte.
        serial_character('\r');
        vga_teletype('\r');
    }
    serial_character(byte);

    if (ansi_state == 2) {  // STATE_CSI
        if (byte == ';') {
            if (ansi_active_param < 2) {
                ansi_active_param = ansi_active_param + 1;
            }
            return;
        }
        if (byte >= '0' && byte <= '9') {
            ansi_params[ansi_active_param] =
                ansi_params[ansi_active_param] * 10 + (byte - '0');
            return;
        }
        // Terminator — dispatch the CSI command.
        ansi_state = 0;
        p1 = ansi_params[0];
        if (p1 == 0) { p1 = 1; }
        p2 = ansi_params[1];
        p3 = ansi_params[2];
        if (byte == '@') {
            // Write character at cursor, no advance.
            vga_write_attribute(p1 & 0xFF, ansi_fg);
        } else if (byte == 'A') {
            // Cursor up
            vga_get_cursor(&dx_packed);
            dx_packed = dx_packed & 0xFFFF;
            row = (dx_packed >> 8) & 0xFF;
            col = dx_packed & 0xFF;
            row = row - p1;
            if (row < 0) { row = 0; }
            vga_set_cursor((row << 8) | col);
        } else if (byte == 'C') {
            // Cursor forward (no row wrap)
            vga_get_cursor(&dx_packed);
            dx_packed = dx_packed & 0xFFFF;
            row = (dx_packed >> 8) & 0xFF;
            col = (dx_packed & 0xFF) + p1;
            vga_set_cursor((row << 8) | (col & 0xFF));
        } else if (byte == 'D') {
            // Cursor back (with row wrap)
            vga_get_cursor(&dx_packed);
            dx_packed = dx_packed & 0xFFFF;
            row = (dx_packed >> 8) & 0xFF;
            col = dx_packed & 0xFF;
            linear = row * 80 + col - p1;
            row = linear / 80;
            col = linear % 80;
            vga_set_cursor((row << 8) | (col & 0xFF));
        } else if (byte == 'H') {
            // Cursor position: row;col (1-indexed)
            row = (p1 - 1) & 0xFF;
            if (p2 == 0) { p2 = 1; }
            col = (p2 - 1) & 0xFF;
            vga_set_cursor((row << 8) | col);
        } else if (byte == 'm') {
            // SGR
            if (ansi_params[0] == 0) {
                ansi_fg = 7;
                vga_set_bg(0);
            } else if (ansi_params[0] == 38) {
                if (p2 == 5) {
                    ansi_fg = p3 & 0xFF;
                }
            } else if (ansi_params[0] == 48) {
                if (p2 == 5) {
                    vga_set_bg(p3 & 0xFF);
                }
            }
        }
        return;
    }

    if (ansi_state == 1) {  // STATE_ESC
        if (byte == '[') {
            ansi_state = 2;
            ansi_params[0] = 0;
            ansi_params[1] = 0;
            ansi_params[2] = 0;
            ansi_active_param = 0;
        } else {
            // Not CSI: emit ESC then the byte
            vga_teletype('\x1B');
            vga_teletype(byte);
            ansi_state = 0;
        }
        return;
    }

    // STATE_NORMAL
    if (byte == '\x1B') {
        ansi_state = 1;
        return;
    }
    vga_teletype(byte);
}
