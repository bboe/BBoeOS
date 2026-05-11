// vga.c — native VGA driver (text + mode 13h).
//
// Replaces drivers/vga.asm.  Five families:
//
//   Cursor  : vga_get_cursor / vga_set_cursor (CRTC index/data pair)
//   Display : vga_clear_screen / vga_scroll_up / vga_teletype /
//             vga_write_attribute (text framebuffer at DIRECT_MAP_BASE + 0xB8000)
//   Mode 13h: vga_fill_block / vga_set_palette_color
//   Mode set: vga_set_mode (programs Misc/Seq/CRTC/GC/AC + DAC + clears FB)
//   ioctl   : fd_ioctl_vga (SYS_IO_IOCTL backend for /dev/vga fds)
//
// Constants (port addresses, register indices) inlined as bare integers
// per the rule shared with rtc.c / fdc.c / ne2k.c — cc.py emits #define
// as %define which would clash with vga.asm's old equ values still in
// the asm-side %include namespace.  Reference table:
//   VGA_CRTC_INDEX        = 0x3D4   text-mode CRTC index port
//   VGA_CRTC_DATA         = 0x3D5   text-mode CRTC data port
//   VGA_CRTC_CURSOR_HIGH  = 0x0E    CRTC register index for cursor high
//   VGA_CRTC_CURSOR_LOW   = 0x0F    CRTC register index for cursor low
//   VGA_INPUT_STATUS_1    = 0x3DA   resets attribute flip-flop
//   VGA_ATTR_WRITE        = 0x3C0   attribute index/data port
//   VGA_AC_OVERSCAN       = 0x11    overscan register index
//   VGA_MISC_WRITE        = 0x3C2   miscellaneous output write port
//   VGA_DAC_INDEX_WRITE   = 0x3C8   DAC palette write-address
//   VGA_DAC_DATA          = 0x3C9   DAC palette R/G/B byte stream
//   VGA_DEFAULT_ATTRIBUTE = 0x07    light-gray on black
//   VGA_COLS              = 80      columns in 80x25 text mode
//   VGA_ROWS              = 25      rows  in 80x25 text mode
//   VGA_MODE_ENTRY_SIZE   = 61      vga_mode_table per-entry length

// `_g_ansi_fg` storage and the bare-name `ansi_fg` shim live in
// drivers/console.c; vga_teletype's asm() block reads it via the
// bare name (which the equ shim resolves at NASM time).

// Re-publish VGA_COLS as an asm-side `equ` so any sibling .asm file
// that still references the bare name (notably the archived
// `console.asm` snapshot, when swapped back in for size measurement)
// can resolve it without needing its own copy.  cc.py-emitted code
// uses the bare integer 80 directly; the equ adds zero bytes to the
// resident kernel.
asm("VGA_COLS equ 80");

// Globals (sorted alphabetically).
//
// vga_current_mode marks the active video mode so vga_mode can skip
// the SR03 flip and FB wipe when the requested mode matches.  Init to
// 0x03 — BIOS leaves us in 80x25 text after boot and vga_font_load
// runs against that state without our mode-table programming.
uint8_t vga_current_mode = 0x03;

// Scrollback ring: 200 rows × 80 cells.  vga_scroll_up pushes the
// about-to-be-discarded row 0 here before scrolling the framebuffer.
// Note: SCROLLBACK_ROWS is defined as a C macro below; inline-asm sites
// use the literal 200 because cc.py does not pass C #define into NASM.
//
// cc.py global arrays support only char, int, uint8_t, or struct element
// types; uint16_t is not supported.  Each VGA cell (char + attribute
// byte) is split into two parallel uint8_t arrays to stay within cc.py's
// constraints while keeping the total BSS small (two 16 KB arrays for
// the ring = 32 KB; two 2 KB arrays for the snapshot = 4 KB).
#define SCROLLBACK_ROWS 200

uint8_t vga_scrollback_ring_char[SCROLLBACK_ROWS * 80];
uint8_t vga_scrollback_ring_attr[SCROLLBACK_ROWS * 80];
int vga_scrollback_head;
int vga_scrollback_valid;
int vga_scrollback_offset;
uint8_t vga_scrollback_snapshot_char[80 * 25];
uint8_t vga_scrollback_snapshot_attr[80 * 25];

// 16-entry default DAC palette (6-bit R, G, B per entry).  Restored on
// every mode switch so mode-13h programs can freely modify the DAC.
uint8_t vga_default_palette[48] = {
     0,  0,  0,    //  0 black
     0,  0, 42,    //  1 dark blue
     0, 42,  0,    //  2 dark green
     0, 42, 42,    //  3 dark cyan
    42,  0,  0,    //  4 dark red
    42,  0, 42,    //  5 dark magenta
    42, 21,  0,    //  6 brown
    42, 42, 42,    //  7 light gray
    21, 21, 21,    //  8 dark gray
    21, 21, 63,    //  9 bright blue
    21, 63, 21,    // 10 bright green
    21, 63, 63,    // 11 bright cyan
    63, 21, 21,    // 12 bright red
    63, 21, 63,    // 13 bright magenta
    63, 63, 21,    // 14 yellow
    63, 63, 63,    // 15 white
};

// VGA mode register tables for vga_set_mode.  Each entry: 1 mode-id +
// 1 misc + 4 seq(1-4) + 25 crtc(0-18h) + 9 gc(0-8) + 21 ac(0-14h) =
// 61 bytes.  Two entries: mode 0x03 (text) and mode 0x13 (graphics).
uint8_t vga_mode_table[122] = {
    // ----- Mode 03h: 80x25 16-colour text, 400 scan lines ---------------
    0x03,                                             // mode ID
    0x67,                                             // Misc Output
    // Sequencer regs 1..4
    0x00, 0x03, 0x05, 0x02,
    // CRTC regs 0x00..0x18
    0x5F, 0x4F, 0x50, 0x82, 0x55, 0x81, 0xBF, 0x1F,
    0x00, 0x4F, 0x0D, 0x0E, 0x00, 0x00, 0x00, 0x00,
    0x9C, 0x8E, 0x8F, 0x28, 0x1F, 0x96, 0xB9, 0xA3,
    0xFF,
    // Graphics Controller regs 0..8
    0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x0E, 0x00, 0xFF,
    // Attribute Controller regs 0x00..0x14
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x14, 0x07,
    0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F,
    0x0C, 0x00, 0x0F, 0x08, 0x00,

    // ----- Mode 13h: 320x200 256-colour ---------------------------------
    0x13,                                             // mode ID
    0x63,                                             // Misc Output
    // Sequencer regs 1..4
    0x01, 0x0F, 0x00, 0x0E,
    // CRTC regs 0x00..0x18
    0x5F, 0x4F, 0x50, 0x82, 0x54, 0x80, 0xBF, 0x1F,
    0x00, 0x41, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x9C, 0x8E, 0x8F, 0x28, 0x40, 0x96, 0xB9, 0xA3,
    0xFF,
    // Graphics Controller regs 0..8
    0x00, 0x00, 0x00, 0x00, 0x00, 0x40, 0x05, 0x0F, 0xFF,
    // Attribute Controller regs 0x00..0x14
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
    0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
    0x41, 0x00, 0x0F, 0x00, 0x00,
};

// vga_set_mode walks `vga_mode_table` until ESI hits this end marker.
// Defined via an asm() block so it lands as a plain label at the byte
// immediately after the table's storage — cc.py would otherwise emit
// a separate `_g_vga_mode_table_end times 0 db 0` slot that NASM
// places later in the output.
asm("_g_vga_mode_table_end equ _g_vga_mode_table + 122");

// fd_ioctl_vga: SYS_IO_IOCTL backend for /dev/vga fds.  Called via
// `jmp` from fd_ioctl, so a normal `ret` returns up to the syscall
// handler's .iret_cf path (CF set/clear translates to user errno).
// AL = cmd, ESI = fd entry, ECX/EDX = command-specific args.  Stays
// as inline asm because the syscall jump-table dispatch enters with
// a register-state contract cc.py's prologue/epilogue would clobber.
void fd_ioctl_vga();

asm("fd_ioctl_vga:\n"
    "        test byte [esi+1], 0x01\n"         // FD_OFFSET_FLAGS, O_WRONLY
    "        jz .fd_ioctl_vga_bad\n"
    "        cmp al, 0x00\n"                    // VGA_IOCTL_FILL_BLOCK
    "        je .fd_ioctl_vga_fill\n"
    "        cmp al, 0x01\n"                    // VGA_IOCTL_MODE
    "        je .fd_ioctl_vga_mode\n"
    "        cmp al, 0x02\n"                    // VGA_IOCTL_SET_PALETTE
    "        je .fd_ioctl_vga_set_palette\n"
    ".fd_ioctl_vga_bad:\n"
    "        stc\n"
    "        ret\n"

    ".fd_ioctl_vga_fill:\n"
    // CL=col, CH=row, DL=color → vga_fill_block expects BL/BH and AL.
    "        mov bx, cx\n"
    "        mov al, dl\n"
    "        call vga_fill_block\n"
    "        clc\n"
    "        ret\n"

    ".fd_ioctl_vga_mode:\n"
    // DL = requested mode.  Send CR + form-feed to the serial console
    // first so external terminals see the mode flip even before VGA
    // catches up.  Skip the full reprogram (and its SR03 flip / FB
    // wipe) when the requested mode already matches the active one;
    // text-mode requests still finish with vga_clear_screen so Ctrl+L
    // visibly clears.
    "        push ax\n"
    "        mov al, 0x0D\n"
    "        call serial_character\n"
    "        mov al, 0x0C\n"
    "        call serial_character\n"
    "        pop ax\n"
    "        mov al, dl\n"
    "        cmp al, [_g_vga_current_mode]\n"
    "        je .fd_ioctl_vga_mode_already\n"
    "        call vga_set_mode\n"               // CF=1 on unsupported
    "        jc .fd_ioctl_vga_mode_done\n"
    "        mov [_g_vga_current_mode], al\n"
    ".fd_ioctl_vga_mode_already:\n"
    "        cmp al, VIDEO_MODE_TEXT_80x25\n"
    "        jne .fd_ioctl_vga_mode_clear_done\n"
    "        call vga_clear_screen\n"
    ".fd_ioctl_vga_mode_clear_done:\n"
    "        clc\n"
    ".fd_ioctl_vga_mode_done:\n"
    "        ret\n"

    ".fd_ioctl_vga_set_palette:\n"
    // CL=index, CH=r, DL=g, DH=b — vga_set_palette_color reads them
    // straight from CX/DX.
    "        call vga_set_palette_color\n"
    "        clc\n"
    "        ret");

// vga_clear_screen: fill the 80x25 text framebuffer with space + default
// attribute (0x0720 word) and home the cursor.  Preserves everything.
// Stays as inline asm — `rep stosw` over 2000 cells beats a per-cell
// `mov word [edi], ax` loop both for runtime and emitted bytes.
void vga_clear_screen();

asm("vga_clear_screen:\n"
    "        push eax\n"
    "        push ecx\n"
    "        push edx\n"
    "        push edi\n"

    "        mov edi, DIRECT_MAP_BASE + 0xB8000\n"
    "        mov ax, 0x0720\n"            // 0x07 attribute, 0x20 ' '
    "        mov ecx, 80 * 25\n"
    "        cld\n"
    "        rep stosw\n"

    "        xor dx, dx\n"
    "        call vga_set_cursor\n"

    "        pop edi\n"
    "        pop edx\n"
    "        pop ecx\n"
    "        pop eax\n"
    "        ret");

// EDI = DIRECT_MAP_BASE + 0xA0000 + row*2560 + col*8 — flat 32-bit linear address.
// Writes 8 rows of 8 pixels each, advancing 320 bytes per row.
void vga_fill_block(uint8_t color __attribute__((in_register("ax"))),
                    int col_row __attribute__((in_register("bx"))))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
    __attribute__((preserve_register("edi")))
{
    int row;
    int col;
    int base;
    int row_index;
    int pixel_index;

    col = col_row & 0xFF;
    row = (col_row >> 8) & 0xFF;
    /* VGA mode-13 framebuffer at phys 0xA0000, kernel-virt =
       DIRECT_MAP_BASE + 0xA0000.  cc.py doesn't resolve NASM equ
       symbols inside C expressions, so the value is folded in here.
       Keep in sync with KERNEL_VIRT_BASE (= DIRECT_MAP_BASE) in
       constants.asm — the inline-asm sites in this file reference
       DIRECT_MAP_BASE symbolically; only this C-side expression
       needs a manual update when the base shifts. */
    base = 0xFF8A0000 + row * 2560 + col * 8;

    row_index = 0;
    while (row_index < 8) {
        pixel_index = 0;
        while (pixel_index < 8) {
            far_write8(base + row_index * 320 + pixel_index, color);
            pixel_index = pixel_index + 1;
        }
        row_index = row_index + 1;
    }
}

// vga_get_cursor: reads CRTC cursor position back into DH:DL packed in
// DX.  drivers/console.c declares this with
// ``__attribute__((out_register("dx")))`` and the ``vga_teletype`` /
// ``vga_write_attribute`` asm() blocks below `call vga_get_cursor`
// directly.  Implementation: index the high/low cursor bytes via
// 0x3D4/0x3D5, then divmod by VGA_COLS to split into row/col.
void vga_get_cursor(int *dx_out __attribute__((out_register("dx"))))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
{
    uint8_t high;
    uint8_t low;
    int linear;
    uint8_t row;
    uint8_t col;

    kernel_outb(0x3D4, 0x0E);
    high = kernel_inb(0x3D5);
    kernel_outb(0x3D4, 0x0F);
    low = kernel_inb(0x3D5);

    linear = (high << 8) | low;
    row = linear / 80;
    col = linear - row * 80;
    *dx_out = (row << 8) | col;
}

// vga_reset_text_mode: idempotent restore of 80x25 text mode.  Called
// from `shell_reload` so that any program that switched the card into
// a graphics mode (e.g. Doom into mode 13h) and exited — cleanly via
// SYS_SYS_EXIT or via a crash through the IDT — leaves the user
// looking at a usable text console rather than a frozen frame of
// pixels.  No-op when the card is already in text mode, so the boot
// fall-through path doesn't clobber the welcome banner.
void vga_reset_text_mode();

asm("vga_reset_text_mode:\n"
    "        cmp byte [_g_vga_current_mode], VIDEO_MODE_TEXT_80x25\n"
    "        je .vga_reset_text_mode_done\n"
    "        push eax\n"
    "        mov al, VIDEO_MODE_TEXT_80x25\n"
    "        call vga_set_mode\n"                    // also clears the framebuffer
    "        mov byte [_g_vga_current_mode], VIDEO_MODE_TEXT_80x25\n"
    "        pop eax\n"
    "        call vga_clear_screen\n"                // homes the cursor
    "    .vga_reset_text_mode_done:\n"
    "        ret");

// vga_scroll_up: scroll the text framebuffer up one row.  The top row is
// discarded; the bottom row is cleared to 0x0720.  Preserves everything.
// Stays as inline asm for the same reason as vga_clear_screen — `rep
// movsw` and `rep stosw` over 80x24 / 80 cells respectively are tight.
void vga_scroll_up();

asm("vga_scroll_up:\n"
    "        push eax\n"
    "        push ebx\n"
    "        push ecx\n"
    "        push edx\n"
    "        push esi\n"
    "        push edi\n"

    /* Push current row 0 into the scrollback ring at head.
       Each VGA cell is a 2-byte word [char, attr].  The ring stores chars
       and attrs in separate uint8_t arrays.  EBX = ring row byte offset. */
    "        mov eax, [_g_vga_scrollback_head]\n"
    "        imul eax, eax, 80\n"
    "        lea edi, [_g_vga_scrollback_ring_char + eax]\n"
    "        mov ebx, eax\n"
    "        lea edx, [_g_vga_scrollback_ring_attr + ebx]\n"
    "        mov esi, DIRECT_MAP_BASE + 0xB8000\n"
    "        mov ecx, 80\n"
    ".vga_scroll_up_ring_push:\n"
    "        mov ax, [esi]\n"        /* ax = char (al) | attr (ah) */
    "        mov [edi], al\n"
    "        mov [edx], ah\n"
    "        add esi, 2\n"
    "        inc edi\n"
    "        inc edx\n"
    "        dec ecx\n"
    "        jnz .vga_scroll_up_ring_push\n"
    /* head = (head + 1) mod SCROLLBACK_ROWS; literal 200 — see #define above */
    "        mov eax, [_g_vga_scrollback_head]\n"
    "        inc eax\n"
    "        cmp eax, 200\n"
    "        jb .vga_scroll_up_head_ok\n"
    "        xor eax, eax\n"
    ".vga_scroll_up_head_ok:\n"
    "        mov [_g_vga_scrollback_head], eax\n"
    /* valid = min(valid + 1, SCROLLBACK_ROWS) */
    "        mov eax, [_g_vga_scrollback_valid]\n"
    "        cmp eax, 200\n"
    "        jae .vga_scroll_up_valid_ok\n"
    "        inc eax\n"
    "        mov [_g_vga_scrollback_valid], eax\n"
    ".vga_scroll_up_valid_ok:\n"

    /* Existing scroll body. */
    "        mov esi, DIRECT_MAP_BASE + 0xB8000 + 80 * 2\n"
    "        mov edi, DIRECT_MAP_BASE + 0xB8000\n"
    "        mov ecx, (25 - 1) * 80\n"
    "        cld\n"
    "        rep movsw\n"

    "        mov edi, DIRECT_MAP_BASE + 0xB8000 + (25 - 1) * 80 * 2\n"
    "        mov ax, 0x0720\n"
    "        mov ecx, 80\n"
    "        rep stosw\n"

    "        pop edi\n"
    "        pop esi\n"
    "        pop edx\n"
    "        pop ecx\n"
    "        pop ebx\n"
    "        pop eax\n"
    "        ret");

// Forward declaration so vga_scrollback_down (and vga_scrollback_up) can
// call vga_scrollback_render which is defined alphabetically after them.
void vga_scrollback_render(int offset);

// vga_scrollback_down: scroll the view down by `rows` rows toward the live
// display.  If new_offset reaches 0 the live framebuffer is restored from
// the snapshot saved by vga_scrollback_up.
void vga_scrollback_down(int rows) {
    int new_offset;
    int row;
    int col;
    if (vga_scrollback_offset == 0) {
        return;
    }
    new_offset = vga_scrollback_offset - rows;
    if (new_offset < 0) {
        new_offset = 0;
    }
    vga_scrollback_offset = new_offset;
    if (new_offset == 0) {
        /* Restore live framebuffer from snapshot. */
        row = 0;
        while (row < 25) {
            col = 0;
            while (col < 80) {
                far_write16(0xFF8B8000 + (row * 80 + col) * 2,
                            vga_scrollback_snapshot_char[row * 80 + col]
                            | (vga_scrollback_snapshot_attr[row * 80 + col] << 8));
                col = col + 1;
            }
            row = row + 1;
        }
        return;
    }
    vga_scrollback_render(new_offset);
}

/* Public predicate so ps2.c can ask "are we currently in scrollback
   mode?" without cracking the offset variable directly. */
int vga_scrollback_is_active() {
    if (vga_scrollback_offset > 0) {
        return 1;
    }
    return 0;
}

/* Render 25 rows of scrollback view at the given offset directly to the
   live VGA framebuffer.  offset == 0 must NOT be passed here — that's
   the live-view restore path (caller copies vga_scrollback_snapshot
   back to 0xB8000).  offset is the number of rows scrolled back from
   the live tail; 1..valid is in-ring, valid+1..valid+24 dips into the
   snapshot's earlier rows. */
void vga_scrollback_render(int offset) {
    int row;
    int virtual_bottom;
    int virtual_top;
    int virtual_index;
    int ring_index;
    int snapshot_index;
    int col;
    int cell;
    int total = vga_scrollback_valid + 25;
    virtual_bottom = total - 1 - offset;
    virtual_top = virtual_bottom - 24;
    row = 0;
    while (row < 25) {
        virtual_index = virtual_top + row;
        col = 0;
        while (col < 80) {
            if (virtual_index < 0) {
                cell = 0x0720;          /* blank with default attribute */
            } else if (virtual_index < vga_scrollback_valid) {
                /* Ring entry: oldest-valid is at (head - valid) mod N,
                   newest-valid is at (head - 1) mod N. */
                ring_index = (vga_scrollback_head - vga_scrollback_valid + virtual_index)
                             % SCROLLBACK_ROWS;
                if (ring_index < 0) {
                    ring_index = ring_index + SCROLLBACK_ROWS;
                }
                cell = vga_scrollback_ring_char[ring_index * 80 + col]
                       | (vga_scrollback_ring_attr[ring_index * 80 + col] << 8);
            } else {
                snapshot_index = virtual_index - vga_scrollback_valid;
                cell = vga_scrollback_snapshot_char[snapshot_index * 80 + col]
                       | (vga_scrollback_snapshot_attr[snapshot_index * 80 + col] << 8);
            }
            far_write16(0xFF8B8000 + (row * 80 + col) * 2, cell);
            col = col + 1;
        }
        row = row + 1;
    }
}

// vga_scrollback_up: scroll the view up by `rows` rows into history.
// On first entry saves the live framebuffer into vga_scrollback_snapshot.
void vga_scrollback_up(int rows) {
    int new_offset;
    int row;
    int col;
    int cell;
    if (vga_scrollback_offset == 0) {
        /* First entry to scrollback — save the live framebuffer. */
        row = 0;
        while (row < 25) {
            col = 0;
            while (col < 80) {
                cell = far_read16(0xFF8B8000 + (row * 80 + col) * 2);
                vga_scrollback_snapshot_char[row * 80 + col] = cell & 0xFF;
                vga_scrollback_snapshot_attr[row * 80 + col] = (cell >> 8) & 0xFF;
                col = col + 1;
            }
            row = row + 1;
        }
    }
    new_offset = vga_scrollback_offset + rows;
    if (new_offset > vga_scrollback_valid) {
        new_offset = vga_scrollback_valid;
    }
    if (new_offset == vga_scrollback_offset) {
        return;     /* already at the top */
    }
    vga_scrollback_offset = new_offset;
    vga_scrollback_render(new_offset);
}

// Reads VGA_INPUT_STATUS_1 first to reset the AC index/data flip-flop
// to "index" state, then writes the (0x11 | 0x20) index byte (bit 5 =
// keep palette latched / video unblanked) and the colour byte to 0x3C0.
void vga_set_bg(uint8_t color __attribute__((in_register("ax"))))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("edx")))
{
    kernel_inb(0x3DA);                 // reset AC flip-flop
    kernel_outb(0x3C0, 0x11 | 0x20);   // AC index = overscan + PAS
    kernel_outb(0x3C0, color);         // colour value
}

// drivers/console.c declares this with ``__attribute__((in_register("dx")))``.
// Clobbers AX; preserves everything else.
void vga_set_cursor(int row_col __attribute__((in_register("dx"))))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
{
    uint8_t row;
    uint8_t col;
    int linear;

    row = (row_col >> 8) & 0xFF;
    col = row_col & 0xFF;
    linear = row * 80 + col;

    kernel_outb(0x3D4, 0x0E);
    kernel_outb(0x3D5, (linear >> 8) & 0xFF);
    kernel_outb(0x3D4, 0x0F);
    kernel_outb(0x3D5, linear & 0xFF);
}

// vga_set_mode: AL = mode.  Programs Misc / Seq / CRTC / GC / AC from
// vga_mode_table, restores the default 16-colour DAC, then clears the
// framebuffer.  CF=1 on unsupported mode.  Preserves all.  Stays as
// inline asm — the lodsb-driven traversal of vga_mode_table is the
// natural shape; expressing it in C would add per-iter call frames
// without producing any genuinely-C-shaped logic.
void vga_set_mode();

asm("vga_set_mode:\n"
    "        push eax\n"
    "        push ebx\n"
    "        push ecx\n"
    "        push edx\n"
    "        push esi\n"
    "        push edi\n"

    "        mov ah, al\n"                       // save requested mode

    "        mov esi, _g_vga_mode_table\n"
    ".vga_set_mode_find:\n"
    "        cmp esi, _g_vga_mode_table_end\n"
    "        jae .vga_set_mode_unsupported\n"
    "        cmp byte [esi], ah\n"
    "        je .vga_set_mode_found\n"
    "        add esi, 61\n"                      // VGA_MODE_ENTRY_SIZE
    "        jmp .vga_set_mode_find\n"

    ".vga_set_mode_unsupported:\n"
    "        stc\n"
    "        jmp .vga_set_mode_done\n"

    ".vga_set_mode_found:\n"
    "        inc esi\n"                          // skip mode-ID byte

    // 1. Misc Output
    "        mov dx, 0x3C2\n"
    "        lodsb\n"
    "        out dx, al\n"

    // 2. Hold sequencer in synchronous reset
    "        mov dx, VGA_SEQ_INDEX\n"
    "        xor al, al\n"
    "        out dx, al\n"
    "        mov dx, VGA_SEQ_DATA\n"
    "        mov al, 0x01\n"
    "        out dx, al\n"

    // 3. Sequencer regs 1..4
    "        mov cx, 4\n"
    "        mov bx, 1\n"
    ".vga_set_mode_seq:\n"
    "        mov dx, VGA_SEQ_INDEX\n"
    "        mov al, bl\n"
    "        out dx, al\n"
    "        mov dx, VGA_SEQ_DATA\n"
    "        lodsb\n"
    "        out dx, al\n"
    "        inc bx\n"
    "        dec cx\n"
    "        jnz .vga_set_mode_seq\n"

    // 4. Release sequencer reset
    "        mov dx, VGA_SEQ_INDEX\n"
    "        xor al, al\n"
    "        out dx, al\n"
    "        mov dx, VGA_SEQ_DATA\n"
    "        mov al, 0x03\n"
    "        out dx, al\n"

    // 5. Unlock CRTC regs 0..7 (clear protect bit in reg 0x11)
    "        mov dx, 0x3D4\n"
    "        mov al, 0x11\n"
    "        out dx, al\n"
    "        mov dx, 0x3D5\n"
    "        in al, dx\n"
    "        and al, 0x7F\n"
    "        out dx, al\n"

    // 6. CRTC regs 0x00..0x18 (25 values)
    "        mov cx, 25\n"
    "        xor bx, bx\n"
    ".vga_set_mode_crtc:\n"
    "        mov dx, 0x3D4\n"
    "        mov al, bl\n"
    "        out dx, al\n"
    "        mov dx, 0x3D5\n"
    "        lodsb\n"
    "        out dx, al\n"
    "        inc bx\n"
    "        dec cx\n"
    "        jnz .vga_set_mode_crtc\n"

    // 7. Graphics Controller regs 0..8 (9 values)
    "        mov cx, 9\n"
    "        xor bx, bx\n"
    ".vga_set_mode_gc:\n"
    "        mov dx, VGA_GC_INDEX\n"
    "        mov al, bl\n"
    "        out dx, al\n"
    "        mov dx, VGA_GC_DATA\n"
    "        lodsb\n"
    "        out dx, al\n"
    "        inc bx\n"
    "        dec cx\n"
    "        jnz .vga_set_mode_gc\n"

    // 8. Attribute Controller regs 0x00..0x14 (21 values)
    "        mov dx, 0x3DA\n"
    "        in al, dx\n"                        // reset AC flip-flop

    "        mov cx, 21\n"
    "        xor bx, bx\n"
    ".vga_set_mode_ac:\n"
    "        mov dx, 0x3C0\n"
    "        mov al, bl\n"
    "        out dx, al\n"
    "        lodsb\n"
    "        out dx, al\n"
    "        inc bx\n"
    "        dec cx\n"
    "        jnz .vga_set_mode_ac\n"

    // 9. Re-enable screen output (PAS bit, bit 5 of AC index write)
    "        mov dx, 0x3C0\n"
    "        mov al, 0x20\n"
    "        out dx, al\n"

    // 10. Restore default 16-colour DAC palette.  Mode 03 BIOS only
    //     populates DAC entries 0-7 / 20 / 56-63; mode 13 indexes the
    //     DAC directly so leftover BIOS state would alias colours.
    "        mov esi, _g_vga_default_palette\n"
    "        mov dx, 0x3C8\n"                   // DAC write-address
    "        xor al, al\n"
    "        out dx, al\n"
    "        inc dx\n"                          // 0x3C9 = DAC data
    "        mov cx, 16 * 3\n"
    "        cld\n"
    ".vga_set_mode_dac:\n"
    "        lodsb\n"
    "        out dx, al\n"
    "        dec cx\n"
    "        jnz .vga_set_mode_dac\n"

    // 11. Clear framebuffer (text → DIRECT_MAP_BASE + 0xB8000 / 0x0720, mode 13 → DIRECT_MAP_BASE + 0xA0000 / 0).
    "        cmp ah, 0x13\n"
    "        je .vga_set_mode_clear_graphics\n"
    "        mov edi, DIRECT_MAP_BASE + 0xB8000\n"
    "        mov ax, 0x0720\n"
    "        mov ecx, 80 * 25\n"
    "        rep stosw\n"
    "        jmp .vga_set_mode_clear_done\n"
    ".vga_set_mode_clear_graphics:\n"
    "        mov edi, DIRECT_MAP_BASE + 0xA0000\n"
    "        xor eax, eax\n"
    "        mov ecx, 320 * 200 / 2\n"
    "        cld\n"
    "        rep stosw\n"
    ".vga_set_mode_clear_done:\n"
    "        clc\n"

    ".vga_set_mode_done:\n"
    "        pop edi\n"
    "        pop esi\n"
    "        pop edx\n"
    "        pop ecx\n"
    "        pop ebx\n"
    "        pop eax\n"
    "        ret");

// DH = B (each 6-bit).  Used by mode-13h programs and the SET_PALETTE
// ioctl path.  Preserves everything.
void vga_set_palette_color(int index_r __attribute__((in_register("cx"))),
                           int g_b __attribute__((in_register("dx"))))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("edx")))
{
    uint8_t index;
    uint8_t r;
    uint8_t g;
    uint8_t b;

    index = index_r & 0xFF;
    r = (index_r >> 8) & 0xFF;
    g = g_b & 0xFF;
    b = (g_b >> 8) & 0xFF;

    kernel_outb(0x3C8, index);
    kernel_outb(0x3C9, r);
    kernel_outb(0x3C9, g);
    kernel_outb(0x3C9, b);
}

// vga_teletype: AL = character.  Uses [ansi_fg] as the attribute byte.
// Handles CR (col=0), LF (row++ with scroll), BS (col--, no wrap),
// otherwise writes char+attr at the cursor and advances.  Preserves all
// registers.  Stays as inline asm — every C cell write would be a
// far_write16 call (push ax / mov ebx, addr / pop ax / mov [ebx], ax)
// plus per-call frame, ballooning the hot character-output path.
void vga_teletype(char byte __attribute__((in_register("ax"))));

asm("vga_teletype:\n"
    "        push eax\n"
    "        push ebx\n"
    "        push ecx\n"
    "        push edx\n"
    "        push edi\n"

    "        cmp al, 0x0D\n"
    "        je .vga_tt_cr\n"
    "        cmp al, 0x0A\n"
    "        je .vga_tt_lf\n"
    "        cmp al, 0x08\n"
    "        je .vga_tt_bs\n"

    // Normal character: stash char/attr, fetch cursor, compute
    // linear FB offset, write the cell, advance.
    "        mov cl, al\n"                       // char
    "        mov ch, [ansi_fg]\n"                // attribute (bare name → equ shim from console.c, or asm-side global if swapped)
    "        call vga_get_cursor\n"              // DH=row, DL=col

    "        movzx eax, dh\n"
    "        imul eax, eax, 80\n"                // row * 80
    "        movzx ebx, dl\n"
    "        add eax, ebx\n"
    "        shl eax, 1\n"                       // byte offset
    "        add eax, DIRECT_MAP_BASE + 0xB8000\n"
    "        mov edi, eax\n"

    "        mov al, cl\n"
    "        mov ah, ch\n"
    "        mov [edi], ax\n"

    "        inc dl\n"
    "        cmp dl, 80\n"
    "        jb .vga_tt_set_cursor\n"
    "        xor dl, dl\n"
    "        inc dh\n"
    "        cmp dh, 25\n"
    "        jb .vga_tt_set_cursor\n"
    "        call vga_scroll_up\n"
    "        mov dh, 24\n"
    ".vga_tt_set_cursor:\n"
    "        call vga_set_cursor\n"
    "        jmp .vga_tt_done\n"

    ".vga_tt_cr:\n"
    "        call vga_get_cursor\n"
    "        xor dl, dl\n"
    "        call vga_set_cursor\n"
    "        jmp .vga_tt_done\n"

    ".vga_tt_lf:\n"
    "        call vga_get_cursor\n"
    "        inc dh\n"
    "        cmp dh, 25\n"
    "        jb .vga_tt_lf_set\n"
    "        call vga_scroll_up\n"
    "        mov dh, 24\n"
    ".vga_tt_lf_set:\n"
    "        call vga_set_cursor\n"
    "        jmp .vga_tt_done\n"

    ".vga_tt_bs:\n"
    "        call vga_get_cursor\n"
    "        test dl, dl\n"
    "        jz .vga_tt_done\n"
    "        dec dl\n"
    "        call vga_set_cursor\n"
    ".vga_tt_done:\n"
    "        pop edi\n"
    "        pop edx\n"
    "        pop ecx\n"
    "        pop ebx\n"
    "        pop eax\n"
    "        ret");

// vga_write_attribute: AL = char, BL = attribute.  Writes at the current
// cursor with no advance.  Used by ANSI colour mid-line.  Preserves all.
void vga_write_attribute(char byte __attribute__((in_register("ax"))),
                         uint8_t attr __attribute__((in_register("bx"))));

asm("vga_write_attribute:\n"
    "        push eax\n"
    "        push ebx\n"
    "        push ecx\n"
    "        push edx\n"
    "        push edi\n"

    "        mov cl, al\n"
    "        mov ch, bl\n"
    "        call vga_get_cursor\n"

    "        movzx eax, dh\n"
    "        imul eax, eax, 80\n"
    "        movzx ebx, dl\n"
    "        add eax, ebx\n"
    "        shl eax, 1\n"
    "        add eax, DIRECT_MAP_BASE + 0xB8000\n"
    "        mov edi, eax\n"

    "        mov al, cl\n"
    "        mov ah, ch\n"
    "        mov [edi], ax\n"

    "        pop edi\n"
    "        pop edx\n"
    "        pop ecx\n"
    "        pop ebx\n"
    "        pop eax\n"
    "        ret");
