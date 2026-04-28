// ansi.c — full ANSI escape sequence parser; unified screen + serial output.
//
// Replaces the asm version of put_character / put_string / serial_character.
// vga_teletype, vga_get_cursor, vga_set_cursor, vga_set_bg, and
// vga_write_attribute remain in drivers/vga.asm (still asm because they
// program the CRTC / attribute controller directly).
//
// Supported escapes (unchanged from the asm version):
//     ESC[nA        cursor up
//     ESC[nC        cursor forward
//     ESC[nD        cursor back
//     ESC[r;cH      cursor position (1-indexed)
//     ESC[0m        reset foreground to 7, background to 0
//     ESC[38;5;Nm   256-color foreground (stored in ansi_fg)
//     ESC[48;5;Nm   256-color background (palette via INT 10h AH=0Bh)
//     ESC[<N>@      write char code N at cursor, no advance or scroll
//
// Calling-convention contract (preserved across the port):
//     put_character(AL=char) — preserves AX, BX, CX, DX
//     put_string(SI=string)  — preserves AX
//     serial_character(AL=byte) — preserves AX, DX
//
// ansi_fg is shared with vga.c's vga_teletype / vga_write_attribute
// (the attribute byte every emitted character carries); it lives here
// as a plain C global and the consumer side aliases _g_ansi_fg.  The
// rest of the parser state (ansi_state, ansi_params, ansi_param_index)
// is internal to this file.

#define ANSI_STATE_NORMAL 0
#define ANSI_STATE_ESC    1
#define ANSI_STATE_CSI    2

#define SERIAL_LSR_PORT   0x3FD
#define SERIAL_LSR_THRE   0x20
#define SERIAL_DATA_PORT  0x3F8

// ANSI_COLS is the screen width used by cursor-back wrapping math.  The
// ``ANSI_`` prefix avoids collision with vga.asm's ``VGA_COLS equ 80`` —
// cc.py emits ``#define`` as a NASM ``%define`` and the macro would later
// expand inside vga.asm to ``80 equ 80`` (same trap that bit ps2.c).
#define ANSI_COLS         80

// ansi_fg: 256-color foreground index, 7 (light gray) at boot.  Plain
// C global; cc.py emits storage as ``_g_ansi_fg`` and vga.c's
// vga_teletype / vga_write_attribute reach it via an
// ``asm_name("_g_ansi_fg")`` alias.
uint8_t ansi_fg = 7;
uint8_t ansi_state;
uint8_t ansi_param_index;
int     ansi_params[3];

// External vga.asm helpers.  AL/BL inputs accept full-int values — the
// asm bodies read only the low byte.  vga_get_cursor packs DH=row /
// DL=col into a single int via the out_register("dx") shim.
void vga_teletype(int byte __attribute__((in_register("ax"))));
void vga_get_cursor(int *cursor __attribute__((out_register("dx"))));
void vga_set_cursor(int cursor __attribute__((in_register("dx"))));
void vga_set_bg(int color __attribute__((in_register("ax"))));
void vga_write_attribute(int byte __attribute__((in_register("ax"))),
                         int attribute __attribute__((in_register("bx"))));

// serial_character: write AL to COM1 after polling LSR.THRE.  Preserves
// AX and DX so put_character (and vga.asm's serial echo path) can call
// it without saving / restoring.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("dx")))
void serial_character(int byte __attribute__((in_register("ax")))) {
    while ((kernel_inb(SERIAL_LSR_PORT) & SERIAL_LSR_THRE) == 0) {
    }
    kernel_outb(SERIAL_DATA_PORT, byte);
}

// put_character: ANSI-aware unified output.  Bytes go to the serial port
// unconditionally (LF prefixed with CR for terminal-friendly newlines)
// and to the screen via vga_teletype, with a small state machine
// (NORMAL → ESC → CSI) decoding ESC[..] sequences.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("bx")))
__attribute__((preserve_register("cx"))) __attribute__((preserve_register("dx")))
void put_character(int byte __attribute__((in_register("ax")))) {
    int al;
    int p1;
    int cursor;
    int row;
    int col;
    int linear;
    int sgr_value;

    al = byte & 0xFF;

    // Convert \n to \r\n on serial; on screen, emit CR then fall through
    // so the LF dispatches normally below (vga_teletype handles row++).
    if (al == 0x0A) {
        serial_character(0x0D);
        vga_teletype(0x0D);
    }
    serial_character(al);

    if (ansi_state == ANSI_STATE_CSI) {
        if (al == ';') {
            // Advance to next param slot, clamped at slot 2 (last).
            if (ansi_param_index < 2) {
                ansi_param_index = ansi_param_index + 1;
            }
            return;
        }
        if (al >= '0' && al <= '9') {
            ansi_params[ansi_param_index] = ansi_params[ansi_param_index] * 10 + (al - '0');
            return;
        }
        // Anything else is the command terminator.  Reset state and
        // dispatch.  p1 defaults to 1 when ansi_params[0] is unset.
        ansi_state = ANSI_STATE_NORMAL;
        p1 = ansi_params[0];
        if (p1 == 0) { p1 = 1; }
        if (al == '@') {
            // ESC[<N>@: write char N at cursor with ansi_fg, no advance.
            vga_write_attribute(p1, ansi_fg);
        } else if (al == 'A') {
            // Cursor up p1 rows, clamped at row 0.
            vga_get_cursor(&cursor);
            row = (cursor >> 8) & 0xFF;
            col = cursor & 0xFF;
            if (row >= p1) {
                row = row - p1;
            } else {
                row = 0;
            }
            vga_set_cursor((row << 8) | col);
        } else if (al == 'C') {
            // Cursor forward p1 cols (no row wrap, matches asm 8-bit add).
            vga_get_cursor(&cursor);
            row = (cursor >> 8) & 0xFF;
            col = (cursor + p1) & 0xFF;
            vga_set_cursor((row << 8) | col);
        } else if (al == 'D') {
            // Cursor back p1 chars; linear math wraps across rows so
            // backspacing past col 0 lands on the prior row's tail.
            vga_get_cursor(&cursor);
            row = (cursor >> 8) & 0xFF;
            col = cursor & 0xFF;
            linear = row * ANSI_COLS + col - p1;
            row = linear / ANSI_COLS;
            col = linear % ANSI_COLS;
            vga_set_cursor((row << 8) | col);
        } else if (al == 'H') {
            // ESC[r;cH: 1-indexed cursor position (default row=col=1).
            row = p1 - 1;
            col = ansi_params[1];
            if (col == 0) { col = 1; }
            col = col - 1;
            vga_set_cursor((row << 8) | col);
        } else if (al == 'm') {
            // SGR: 0=reset, 38;5;N=fg color, 48;5;N=bg color.
            sgr_value = ansi_params[0];
            if (sgr_value == 0) {
                ansi_fg = 7;
                vga_set_bg(0);
            } else if (sgr_value == 38) {
                if (ansi_params[1] == 5) {
                    ansi_fg = ansi_params[2] & 0xFF;
                }
            } else if (sgr_value == 48) {
                if (ansi_params[1] == 5) {
                    vga_set_bg(ansi_params[2] & 0xFF);
                }
            }
        }
    } else if (ansi_state == ANSI_STATE_ESC) {
        if (al == '[') {
            // Enter CSI: zero param accumulators.
            ansi_state = ANSI_STATE_CSI;
            ansi_params[0] = 0;
            ansi_params[1] = 0;
            ansi_params[2] = 0;
            ansi_param_index = 0;
        } else {
            // Not a CSI introducer: replay ESC + this char to the screen
            // and drop back to NORMAL.  Serial already saw both bytes.
            vga_teletype(0x1B);
            vga_teletype(al);
            ansi_state = ANSI_STATE_NORMAL;
        }
    } else {
        // NORMAL state: ESC starts a sequence; everything else prints.
        if (al == 0x1B) {
            ansi_state = ANSI_STATE_ESC;
        } else {
            vga_teletype(al);
        }
    }
}

// put_string: print null-terminated string at SI via put_character.
// Preserves AX (asm callers expect the char they pushed to be intact);
// SI on return points at the null terminator (the asm version's lodsb
// landed one past it, but no caller relies on that).
__attribute__((preserve_register("ax")))
void put_string(uint8_t *string __attribute__((in_register("si")))) {
    while (string[0] != 0) {
        put_character(string[0]);
        string = string + 1;
    }
}

