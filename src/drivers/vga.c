// vga.c — native VGA text-mode + mode-13h driver.
//
// Replaces the INT 10h subroutines the ANSI parser (ansi.c) and the
// SYS_IO_IOCTL handler relied on:
//     AH=0Eh teletype          → vga_teletype       (AL = char)
//     AH=03h get cursor        → vga_get_cursor     (returns DH=row, DL=col)
//     AH=02h set cursor        → vga_set_cursor     (DH=row, DL=col)
//     AH=09h char+attribute    → vga_write_attribute (AL=char, BL=attr)
//     AH=0Bh BH=0 overscan     → vga_set_bg         (AL = color)
//     AH=00h AL=03h reset text → vga_clear_screen
//
// 80x25 text mode assumed.  Text buffer at segment 0xB800 offset 0,
// with 2 bytes per cell (char, attribute).  Cursor tracked in CRTC
// registers (I/O 0x3D4 index, 0x3D5 data): index 0x0E = position high
// byte, 0x0F = position low byte.  Overscan colour is AC register 0x11.
//
// The framebuffer (0xB800 / 0xA000) sits past the 64 KB DS reach in
// 16-bit real mode, so every byte that lands there has to go through
// an ES segment override that cc.py's pure C can't emit — those tight
// ``mov es,ax / xor di,di / rep stosw`` sequences (and the BIOS INT 10h
// that fetches the ROM font) live in the file-scope asm() block at the
// bottom of this file as a small set of helpers.  Everything else —
// CRTC / AC / SR / GC port banging, mode-table dispatch, the ANSI-
// teletype state machine — is straight C against kernel_inb /
// kernel_outb.

// VGA_* port and command constants.  Prefix avoids cc.py's
// ``%define VGA_X``-vs-NASM ``%assign VGA_X equ ...`` collision (we
// don't reuse any include/constants.asm names here, but the prefix
// also documents the namespace cleanly).
#define VGA_COLS              80
#define VGA_ROWS              25
#define VGA_SEG               0xB800
#define VGA_SEG_GRAPHICS      0xA000
#define VGA_DEFAULT_ATTRIBUTE 0x07

#define VGA_AC_OVERSCAN       0x11
#define VGA_ATTR_WRITE        0x3C0
#define VGA_CRTC_CURSOR_HIGH  0x0E
#define VGA_CRTC_CURSOR_LOW   0x0F
#define VGA_CRTC_DATA         0x3D5
#define VGA_CRTC_INDEX        0x3D4
#define VGA_DAC_INDEX         0x3C8
#define VGA_DAC_DATA          0x3C9
#define VGA_GC_DATA           0x3CF
#define VGA_GC_INDEX          0x3CE
#define VGA_INPUT_STATUS_1    0x3DA
#define VGA_MISC_WRITE        0x3C2
#define VGA_SEQ_DATA          0x3C5
#define VGA_SEQ_INDEX         0x3C4

// vga_current_mode: which video mode we last programmed.  Initialised
// to VIDEO_MODE_TEXT_80x25 because the BIOS leaves us in 80x25 text
// after stage 1 — vga_font_load runs against that state without
// touching our register table.  fd_ioctl_vga's mode handler skips the
// full register reprogram (and SR03 flip) when the requested mode
// already matches.
uint8_t vga_current_mode = VIDEO_MODE_TEXT_80x25;

// ansi_fg lives in ansi.c as a plain C global; we read it through a
// _g_-prefixed asm_name alias.  vga_teletype and vga_write_attribute
// stamp it as the high byte of every cell they write.
uint8_t ansi_fg __attribute__((asm_name("_g_ansi_fg")));

// Forward declarations for the asm-only helpers at the bottom of this
// file.  Each one wraps a short framebuffer or BIOS access that pure
// C can't emit (ES segment override, INT 10h call, planar VGA write
// path).  Calling-convention attributes use cc.py's in_register so
// the C call site loads the parameters into the registers the asm
// body expects.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("cx")))
__attribute__((preserve_register("di"))) __attribute__((preserve_register("es")))
void vga_text_clear_screen(int word_value __attribute__((in_register("ax"))));

__attribute__((preserve_register("ax"))) __attribute__((preserve_register("cx")))
__attribute__((preserve_register("si"))) __attribute__((preserve_register("di")))
__attribute__((preserve_register("ds"))) __attribute__((preserve_register("es")))
void vga_text_scroll_up();

__attribute__((preserve_register("ax"))) __attribute__((preserve_register("di")))
__attribute__((preserve_register("es")))
void vga_text_putw(int byte_offset __attribute__((in_register("di"))),
                   int word_value __attribute__((in_register("ax"))));

__attribute__((preserve_register("ax"))) __attribute__((preserve_register("cx")))
__attribute__((preserve_register("di"))) __attribute__((preserve_register("es")))
void vga_graphics_clear_screen();

__attribute__((preserve_register("ax"))) __attribute__((preserve_register("cx")))
__attribute__((preserve_register("di"))) __attribute__((preserve_register("es")))
void vga_graphics_fill_8x8(int byte_offset __attribute__((in_register("di"))),
                           int color __attribute__((in_register("ax"))));

void vga_font_load();

// serial_character: drivers/ansi.c.  Echoes the char to COM1.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("dx")))
void serial_character(int byte __attribute__((in_register("ax"))));

// vga_default_palette: BIOS mode-3 palette restored on every mode set.
// 16 entries of (R, G, B), 6 bits per channel.  cc.py emits storage as
// _g_vga_default_palette.
uint8_t vga_default_palette[48] = {
     0,  0,  0,    0,  0, 42,    0, 42,  0,    0, 42, 42,
    42,  0,  0,   42,  0, 42,   42, 21,  0,   42, 42, 42,
    21, 21, 21,   21, 21, 63,   21, 63, 21,   21, 63, 63,
    63, 21, 21,   63, 21, 63,   63, 63, 21,   63, 63, 63,
};

// vga_mode_table: register sequences for the two supported modes.
// The mode-id byte from the asm version is dropped (mode dispatch is
// in C now), so each entry is a flat 60 bytes:
//     [0]    Misc Output
//     [1..4] Sequencer regs 1-4
//     [5..29]  CRTC regs 0x00-0x18 (25 values)
//     [30..38] Graphics Controller regs 0x00-0x08 (9 values)
//     [39..59] Attribute Controller regs 0x00-0x14 (21 values)
uint8_t vga_mode_table[120] = {
    // ---- Mode 03h: 80x25 16-colour text, 400 scan lines ----
    0x67,                               // Misc Output
    0x00, 0x03, 0x05, 0x02,             // Sequencer 1-4
    // CRTC 0x00-0x18: 9-dot horizontal, font in plane 2 + 0x4000
    0x5F, 0x4F, 0x50, 0x82, 0x55, 0x81, 0xBF, 0x1F,
    0x00, 0x4F, 0x0D, 0x0E, 0x00, 0x00, 0x00, 0x00,
    0x9C, 0x8E, 0x8F, 0x28, 0x1F, 0x96, 0xB9, 0xA3,
    0xFF,
    // GC 0x00-0x08
    0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x0E, 0x00, 0xFF,
    // AC 0x00-0x14: palette indices 0-15, then mode/overscan/planes/pan/select
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x14, 0x07,
    0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F,
    0x0C, 0x00, 0x0F, 0x08, 0x00,

    // ---- Mode 13h: 320x200 256-colour ----
    0x63,                               // Misc Output
    0x01, 0x0F, 0x00, 0x0E,             // Sequencer 1-4 (chain4, all planes)
    // CRTC 0x00-0x18
    0x5F, 0x4F, 0x50, 0x82, 0x54, 0x80, 0xBF, 0x1F,
    0x00, 0x41, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x9C, 0x8E, 0x8F, 0x28, 0x40, 0x96, 0xB9, 0xA3,
    0xFF,
    // GC 0x00-0x08 (graphics mode, 8-bit colour)
    0x00, 0x00, 0x00, 0x00, 0x00, 0x40, 0x05, 0x0F, 0xFF,
    // AC 0x00-0x14: identity palette + 256-colour mode bit
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
    0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
    0x41, 0x00, 0x0F, 0x00, 0x00,
};

// vga_get_cursor: return DH = row, DL = col by reading the CRTC
// cursor-position registers (0x0E high, 0x0F low) and dividing by 80.
// out_register("dx") gives the asm-side caller (ansi.c via the
// in/out_register("dx")) the row<<8|col packing it expects.
__attribute__((preserve_register("bx"))) __attribute__((preserve_register("cx")))
void vga_get_cursor(int *cursor __attribute__((out_register("dx")))) {
    int linear;
    int row;
    int col;
    kernel_outb(VGA_CRTC_INDEX, VGA_CRTC_CURSOR_HIGH);
    linear = kernel_inb(VGA_CRTC_DATA) << 8;
    kernel_outb(VGA_CRTC_INDEX, VGA_CRTC_CURSOR_LOW);
    linear = linear | kernel_inb(VGA_CRTC_DATA);
    row = linear / VGA_COLS;
    col = linear % VGA_COLS;
    *cursor = (row << 8) | col;
}

// vga_set_cursor: write DH = row / DL = col into the CRTC cursor-position
// registers.  The packed parameter matches vga_get_cursor's output and
// the in_register("dx") contract callers in ansi.c rely on.
__attribute__((preserve_register("bx"))) __attribute__((preserve_register("cx")))
__attribute__((preserve_register("dx")))
void vga_set_cursor(int cursor __attribute__((in_register("dx")))) {
    int row;
    int col;
    int linear;
    row = (cursor >> 8) & 0xFF;
    col = cursor & 0xFF;
    linear = row * VGA_COLS + col;
    kernel_outb(VGA_CRTC_INDEX, VGA_CRTC_CURSOR_HIGH);
    kernel_outb(VGA_CRTC_DATA, (linear >> 8) & 0xFF);
    kernel_outb(VGA_CRTC_INDEX, VGA_CRTC_CURSOR_LOW);
    kernel_outb(VGA_CRTC_DATA, linear & 0xFF);
}

// vga_set_bg: set the AC overscan colour (border + extended-background).
// IS1 read resets the AC flip-flop to "index"; we then write index|0x20
// (PAS bit keeps the palette active) followed by the colour byte.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("dx")))
void vga_set_bg(int color __attribute__((in_register("ax")))) {
    kernel_inb(VGA_INPUT_STATUS_1);
    kernel_outb(VGA_ATTR_WRITE, VGA_AC_OVERSCAN | 0x20);
    kernel_outb(VGA_ATTR_WRITE, color);
}

// vga_set_palette_color: program one DAC entry to (r, g, b) in 6-bit RGB.
// Input register layout matches the asm version: CL=index, CH=r, DL=g,
// DH=b — packed into two int parameters for the C side.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("bx")))
__attribute__((preserve_register("dx")))
void vga_set_palette_color(int index_red __attribute__((in_register("cx"))),
                           int green_blue __attribute__((in_register("dx")))) {
    int index;
    int red;
    int green;
    int blue;
    index = index_red & 0xFF;
    red = (index_red >> 8) & 0xFF;
    green = green_blue & 0xFF;
    blue = (green_blue >> 8) & 0xFF;
    kernel_outb(VGA_DAC_INDEX, index);
    kernel_outb(VGA_DAC_DATA, red);
    kernel_outb(VGA_DAC_DATA, green);
    kernel_outb(VGA_DAC_DATA, blue);
}

// vga_clear_screen: blank the text framebuffer to space + default
// attribute, move the cursor to (0, 0).  Wraps the asm helper which
// runs the actual ``rep stosw`` against ES = 0xB800.
void vga_clear_screen() {
    vga_text_clear_screen((VGA_DEFAULT_ATTRIBUTE << 8) | ' ');
    vga_set_cursor(0);
}

// vga_teletype: write one character at the cursor position, advancing
// or scrolling as needed.  CR resets col to 0; LF advances row (with
// scroll on overflow); BS decrements col with no wrap.  Other bytes
// land in the framebuffer with ansi_fg as their attribute.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("bx")))
__attribute__((preserve_register("cx"))) __attribute__((preserve_register("dx")))
void vga_teletype(int byte __attribute__((in_register("ax")))) {
    int al;
    int cursor;
    int row;
    int col;
    int byte_offset;
    al = byte & 0xFF;
    if (al == 0x0D) {
        // CR: col ← 0
        vga_get_cursor(&cursor);
        row = (cursor >> 8) & 0xFF;
        vga_set_cursor(row << 8);
        return;
    }
    if (al == 0x0A) {
        // LF: row++, scroll on overflow
        vga_get_cursor(&cursor);
        row = (cursor >> 8) & 0xFF;
        col = cursor & 0xFF;
        row = row + 1;
        if (row >= VGA_ROWS) {
            vga_text_scroll_up();
            row = VGA_ROWS - 1;
        }
        vga_set_cursor((row << 8) | col);
        return;
    }
    if (al == 0x08) {
        // BS: col-- (no wrap)
        vga_get_cursor(&cursor);
        col = cursor & 0xFF;
        if (col == 0) { return; }
        cursor = cursor - 1;
        vga_set_cursor(cursor);
        return;
    }
    // Normal char: write at cursor, advance.
    vga_get_cursor(&cursor);
    row = (cursor >> 8) & 0xFF;
    col = cursor & 0xFF;
    byte_offset = (row * VGA_COLS + col) << 1;
    vga_text_putw(byte_offset, (ansi_fg << 8) | al);
    col = col + 1;
    if (col >= VGA_COLS) {
        col = 0;
        row = row + 1;
        if (row >= VGA_ROWS) {
            vga_text_scroll_up();
            row = VGA_ROWS - 1;
        }
    }
    vga_set_cursor((row << 8) | col);
}

// vga_write_attribute: place one (char, attribute) pair at the current
// cursor without advancing.  Used by the ANSI ``ESC[<N>@`` escape.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("bx")))
__attribute__((preserve_register("cx"))) __attribute__((preserve_register("dx")))
void vga_write_attribute(int byte __attribute__((in_register("ax"))),
                         int attribute __attribute__((in_register("bx")))) {
    int cursor;
    int row;
    int col;
    int byte_offset;
    int word_value;
    vga_get_cursor(&cursor);
    row = (cursor >> 8) & 0xFF;
    col = cursor & 0xFF;
    byte_offset = (row * VGA_COLS + col) << 1;
    word_value = ((attribute & 0xFF) << 8) | (byte & 0xFF);
    vga_text_putw(byte_offset, word_value);
}

// vga_fill_block: paint an 8x8 tile at (col, row) on the mode-13h
// framebuffer with the given colour.  Tile coordinates: 0-39 cols ×
// 0-24 rows.  Byte offset = row*2560 + col*8 (each tile-row spans
// 320 pixels).
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("bx")))
__attribute__((preserve_register("cx"))) __attribute__((preserve_register("dx")))
void vga_fill_block(int color_ax __attribute__((in_register("ax"))),
                    int row_col __attribute__((in_register("bx")))) {
    int row;
    int col;
    int byte_offset;
    int color;
    color = color_ax & 0xFF;
    row = (row_col >> 8) & 0xFF;
    col = row_col & 0xFF;
    byte_offset = row * 2560 + col * 8;
    vga_graphics_fill_8x8(byte_offset, color);
}

// vga_set_mode: program every VGA register from the matching mode-table
// entry, restore the default DAC palette, and clear the framebuffer.
// Returns CF set on unsupported mode.  The framebuffer clear at the
// end dispatches to the right asm helper for the selected mode (text
// mode-3 zaps ES=0xB800 with space+attr, mode-13h zaps ES=0xA000).
__attribute__((carry_return))
int vga_set_mode(int mode __attribute__((in_register("ax")))) {
    int requested;
    int entry_offset;
    uint8_t *entry;
    int i;
    int crtc_protect;
    requested = mode & 0xFF;
    if (requested == VIDEO_MODE_TEXT_80x25) {
        entry_offset = 0;
    } else if (requested == VIDEO_MODE_VGA_320x200_256) {
        entry_offset = 60;
    } else {
        return 0;
    }
    entry = vga_mode_table + entry_offset;

    // 1. Miscellaneous Output.
    kernel_outb(VGA_MISC_WRITE, entry[0]);

    // 2. Sequencer: hold in synchronous reset (SR00 = 1).
    kernel_outb(VGA_SEQ_INDEX, 0x00);
    kernel_outb(VGA_SEQ_DATA, 0x01);

    // 3. Sequencer regs 1-4.
    i = 0;
    while (i < 4) {
        kernel_outb(VGA_SEQ_INDEX, i + 1);
        kernel_outb(VGA_SEQ_DATA, entry[1 + i]);
        i = i + 1;
    }

    // 4. Release sequencer reset (SR00 = 3).
    kernel_outb(VGA_SEQ_INDEX, 0x00);
    kernel_outb(VGA_SEQ_DATA, 0x03);

    // 5. Unlock CRTC regs 0-7 (clear bit 7 of CRTC reg 0x11).
    kernel_outb(VGA_CRTC_INDEX, 0x11);
    crtc_protect = kernel_inb(VGA_CRTC_DATA);
    kernel_outb(VGA_CRTC_DATA, crtc_protect & 0x7F);

    // 6. CRTC regs 0x00-0x18 (25 values).
    i = 0;
    while (i < 25) {
        kernel_outb(VGA_CRTC_INDEX, i);
        kernel_outb(VGA_CRTC_DATA, entry[5 + i]);
        i = i + 1;
    }

    // 7. Graphics Controller regs 0x00-0x08 (9 values).
    i = 0;
    while (i < 9) {
        kernel_outb(VGA_GC_INDEX, i);
        kernel_outb(VGA_GC_DATA, entry[30 + i]);
        i = i + 1;
    }

    // 8. Attribute Controller regs 0x00-0x14 (21 values).  IS1 read
    // resets the AC flip-flop; index/data alternate to ATTR_WRITE.
    kernel_inb(VGA_INPUT_STATUS_1);
    i = 0;
    while (i < 21) {
        kernel_outb(VGA_ATTR_WRITE, i);
        kernel_outb(VGA_ATTR_WRITE, entry[39 + i]);
        i = i + 1;
    }

    // 9. Re-enable screen output (PAS bit, AC index = 0x20).
    kernel_outb(VGA_ATTR_WRITE, 0x20);

    // 10. Restore default 16-colour DAC palette.  Mode 3 only populates
    // 8 + 1 + 8 entries via its AC palette, so mode-13h programs would
    // see DAC[8..15] at whatever garbage BIOS left if we skipped this.
    kernel_outb(VGA_DAC_INDEX, 0x00);
    i = 0;
    while (i < 48) {
        kernel_outb(VGA_DAC_DATA, vga_default_palette[i]);
        i = i + 1;
    }

    // 11. Clear framebuffer.
    if (requested == VIDEO_MODE_VGA_320x200_256) {
        vga_graphics_clear_screen();
    } else {
        vga_text_clear_screen((VGA_DEFAULT_ATTRIBUTE << 8) | ' ');
    }
    return 1;
}

// fd_ioctl_vga: SYS_IO_IOCTL entry for /dev/vga fds.  AL = command,
// SI = fd entry; CX (row_col) and DX (dx_arg) carry the per-cmd args
// the user loaded before INT 30h.  Refuses non-O_WRONLY fds (every
// ioctl mutates device state).  Returns CF set on unsupported command
// or mode; called from fd_ioctl, whose ret lands back in the syscall
// handler's .iret_cf path.
struct fd {
    uint8_t type;
    uint8_t flags;
    uint8_t _rest[30];
};

__attribute__((carry_return))
int fd_ioctl_vga(struct fd *entry __attribute__((in_register("si"))),
                 int ioctl_cmd __attribute__((in_register("ax"))),
                 int row_col __attribute__((in_register("cx"))),
                 int dx_arg __attribute__((in_register("dx")))) {
    int cmd;
    int requested_mode;
    cmd = ioctl_cmd & 0xFF;
    if ((entry->flags & O_WRONLY) == 0) { return 0; }
    if (cmd == VGA_IOCTL_FILL_BLOCK) {
        // CL=col, CH=row → row_col packed (high=row, low=col).
        // DL=color → dx_arg low byte.
        vga_fill_block(dx_arg & 0xFF, row_col);
        return 1;
    }
    if (cmd == VGA_IOCTL_MODE) {
        // DL = requested mode.  Echo CR + form-feed to serial first
        // (so terminal-side scrollback resets), then reprogram only
        // when the mode actually changes — avoids the SR03 flip and
        // framebuffer clear on Ctrl+L hits that don't flip modes.
        serial_character('\r');
        serial_character(0x0C);
        requested_mode = dx_arg & 0xFF;
        if (requested_mode != vga_current_mode) {
            if (!vga_set_mode(requested_mode)) { return 0; }
            vga_current_mode = requested_mode;
        }
        if (requested_mode == VIDEO_MODE_TEXT_80x25) { vga_clear_screen(); }
        return 1;
    }
    if (cmd == VGA_IOCTL_SET_PALETTE) {
        // CL=index, CH=r → row_col; DL=g, DH=b → dx_arg.
        vga_set_palette_color(row_col, dx_arg);
        return 1;
    }
    return 0;
}

// --- Asm helpers for framebuffer access ---
//
// All of these touch ES = 0xB800 or 0xA000, which cc.py's pure C path
// can't reach in 16-bit real mode (DS=0 only covers 0..0xFFFF, and
// 0xB8000 / 0xA0000 sit above that).  Each helper saves + restores
// every register / segment it touches so the C call sites can treat
// them as opaque "fill / scroll / putw / load font" primitives.
//
// vga_text_clear_screen   AX = (attr << 8) | char.  Writes 80*25 cells.
// vga_text_scroll_up      Move rows 1..24 → 0..23, fill row 24 with default.
// vga_text_putw           DI = byte offset within 0xB800; AX = word.
// vga_graphics_clear_screen  Zero 320*200 bytes at 0xA000.
// vga_graphics_fill_8x8    DI = byte offset within 0xA000; AL = colour.
// vga_font_load           BIOS INT 10h ROM-font copy into plane 2 + 0x4000.
asm("
vga_text_clear_screen:
        push ax
        push cx
        push di
        push es
        mov cx, ax
        mov ax, 0xB800
        mov es, ax
        mov ax, cx
        xor di, di
        mov cx, 80*25
        cld
        rep stosw
        pop es
        pop di
        pop cx
        pop ax
        ret

vga_text_scroll_up:
        push ax
        push cx
        push si
        push di
        push ds
        push es
        mov ax, 0xB800
        mov ds, ax
        mov es, ax
        mov si, 80*2
        xor di, di
        mov cx, 24*80
        cld
        rep movsw
        mov di, 24*80*2
        mov ax, (0x07 << 8) | ' '
        mov cx, 80
        rep stosw
        pop es
        pop ds
        pop di
        pop si
        pop cx
        pop ax
        ret

vga_text_putw:
        push ax
        push di
        push es
        mov cx, ax
        mov ax, 0xB800
        mov es, ax
        mov ax, cx
        mov [es:di], ax
        pop es
        pop di
        pop ax
        ret

vga_graphics_clear_screen:
        push ax
        push cx
        push di
        push es
        mov ax, 0xA000
        mov es, ax
        xor di, di
        xor ax, ax
        mov cx, 320*200/2
        cld
        rep stosw
        pop es
        pop di
        pop cx
        pop ax
        ret

vga_graphics_fill_8x8:
        push ax
        push cx
        push di
        push es
        mov bx, ax
        mov ax, 0xA000
        mov es, ax
        mov ax, bx
        mov cx, 8
        cld
.fill_block_row:
        push di
        push cx
        mov cx, 8
        rep stosb
        pop cx
        pop di
        add di, 320
        dec cx
        jnz .fill_block_row
        pop es
        pop di
        pop cx
        pop ax
        ret

;; vga_font_load: copy the BIOS ROM 8x16 font into plane 2 + 0x4000.
;; INT 10h AH=11h AL=30h BH=06h is a query-only BIOS subfunction that
;; returns ES:BP pointing to the ROM font — the load subfunctions
;; (AL=04h/14h/00h) touch VGA state and are unreliable in QEMU when
;; called outside the BIOS mode-set path, so we drive the planar
;; write ourselves to copy 256 glyphs * 16 bytes (zero-padded out to
;; the VGA's fixed 32-byte slot size) into the char-gen.
;;
;; Plane 2 offset 0x4000 is chosen because mode 13h's framebuffer
;; clear writes zeros to plane 2 bytes 0..15999 (plane 2 is shared
;; between mode 13h and the char-gen); offset 0x4000 sits above that
;; range and survives every switch.  The matching SR03 = 0x05 in the
;; mode-3 table routes both char sets there.
;;
;; Every GC register the planar write path consults has to be
;; explicitly initialised: GR01 (enable set/reset) leaving plane 2
;; non-zero or GR03 (data rotate / logic op) non-zero would cause the
;; copy to silently emit a uniform pattern instead of CPU data.
vga_font_load:
        push ax
        push bx
        push cx
        push dx
        push si
        push di
        push bp
        push ds
        push es

        mov ax, 0x1130
        mov bh, 0x06
        int 0x10
        push es
        pop ds
        mov si, bp

        mov dx, 0x3C4
        mov al, 0x04
        out dx, al
        mov dx, 0x3C5
        mov al, 0x06
        out dx, al

        mov dx, 0x3C4
        mov al, 0x02
        out dx, al
        mov dx, 0x3C5
        mov al, 0x04
        out dx, al

        mov dx, 0x3CE
        mov al, 0x00
        out dx, al
        mov dx, 0x3CF
        xor al, al
        out dx, al

        mov dx, 0x3CE
        mov al, 0x01
        out dx, al
        mov dx, 0x3CF
        xor al, al
        out dx, al

        mov dx, 0x3CE
        mov al, 0x03
        out dx, al
        mov dx, 0x3CF
        xor al, al
        out dx, al

        mov dx, 0x3CE
        mov al, 0x05
        out dx, al
        mov dx, 0x3CF
        xor al, al
        out dx, al

        mov dx, 0x3CE
        mov al, 0x06
        out dx, al
        mov dx, 0x3CF
        mov al, 0x05
        out dx, al

        mov dx, 0x3CE
        mov al, 0x08
        out dx, al
        mov dx, 0x3CF
        mov al, 0xFF
        out dx, al

        mov ax, 0xA000
        mov es, ax
        mov di, 0x4000
        mov bx, 256
        cld
.fl_char_loop:
        mov cx, 8
        rep movsw
        mov cx, 8
        xor ax, ax
        rep stosw
        dec bx
        jnz .fl_char_loop

        mov dx, 0x3C4
        mov al, 0x04
        out dx, al
        mov dx, 0x3C5
        mov al, 0x02
        out dx, al

        mov dx, 0x3C4
        mov al, 0x02
        out dx, al
        mov dx, 0x3C5
        mov al, 0x03
        out dx, al

        mov dx, 0x3CE
        mov al, 0x05
        out dx, al
        mov dx, 0x3CF
        mov al, 0x10
        out dx, al

        mov dx, 0x3CE
        mov al, 0x06
        out dx, al
        mov dx, 0x3CF
        mov al, 0x0E
        out dx, al

        pop es
        pop ds
        pop bp
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        ret
");
