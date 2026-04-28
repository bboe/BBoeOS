;;; ------------------------------------------------------------------------
;;; vga.asm — native VGA text-mode driver.
;;;
;;; Replaces the INT 10h subroutines the ANSI parser (console.asm) and the
;;; SYS_VIDEO_MODE handler relied on:
;;;     AH=0Eh teletype          → vga_teletype       (AL = char)
;;;     AH=03h get cursor        → vga_get_cursor     (returns DH=row, DL=col)
;;;     AH=02h set cursor        → vga_set_cursor     (DH=row, DL=col)
;;;     AH=09h char+attribute    → vga_write_attribute (AL=char, BL=attr)
;;;     AH=0Bh BH=0 overscan     → vga_set_bg         (AL = color)
;;;     AH=00h AL=03h reset text → vga_clear_screen
;;;
;;; 80x25 text mode assumed.  Text buffer at segment 0xB800 offset 0, with
;;; 2 bytes per cell (char, attribute).  Cursor tracked in CRTC registers
;;; (I/O 0x3D4 index, 0x3D5 data): index 0x0E = position high byte,
;;; 0x0F = position low byte.  Overscan colour is AC register 0x11.
;;; ------------------------------------------------------------------------

        VGA_AC_OVERSCAN         equ 11h
        VGA_ATTR_WRITE          equ 03C0h
        VGA_COLS                equ 80
        VGA_CRTC_CURSOR_HIGH    equ 0Eh
        VGA_CRTC_CURSOR_LOW     equ 0Fh
        VGA_CRTC_DATA           equ 03D5h
        VGA_CRTC_INDEX          equ 03D4h
        VGA_DEFAULT_ATTRIBUTE   equ 07h
        VGA_INPUT_STATUS_1      equ 03DAh
        VGA_MISC_WRITE          equ 03C2h
        VGA_MODE_ENTRY_SIZE     equ 61  ; 1 mode-id + 1 misc + 4 seq + 25 crtc + 9 gc + 21 ac
        VGA_ROWS                equ 25
        VGA_SEG                 equ 0B800h

vga_clear_screen:
        ;; Fills the text buffer with space + default attribute and moves
        ;; the cursor to (0,0).  Preserves all registers.
        push ax
        push ecx
        push dx
        push edi

        mov edi, 0xB8000
        mov ax, (VGA_DEFAULT_ATTRIBUTE << 8) | ' '
        mov ecx, VGA_COLS * VGA_ROWS
        cld
        rep stosw

        xor dx, dx              ; DH=0 row, DL=0 col
        call vga_set_cursor

        pop edi
        pop dx
        pop ecx
        pop ax
        ret

vga_fill_block:
        ;; Fill an 8×8 tile at (BL=col, BH=row) with color AL in mode 13h.
        ;; Tile coordinates: 0-39 columns, 0-24 rows.  Preserves all registers.
        ;; Uses flat 32-bit addressing — DS already covers the linear VGA
        ;; framebuffer at 0xA0000, no ES reload (a real-mode segment value
        ;; would #GP in protected mode).
        push eax
        push ebx
        push ecx
        push edx
        push edi

        mov cl, al                      ; stash color

        ;; EDI = 0xA0000 + row * 2560 + col * 8  (linear pixel offset)
        movzx edi, bh                   ; EDI = row
        imul edi, 2560                  ; EDI = row × 2560
        movzx eax, bl                   ; EAX = col
        shl eax, 3                      ; EAX = col × 8
        add edi, eax
        add edi, 0xA0000                ; flat VGA framebuffer base

        mov al, cl                      ; restore color

        mov ecx, 8                      ; 8 tile rows
        cld
.fill_row:
        push edi
        push ecx
        mov ecx, 8
        rep stosb                       ; write 8 pixels with color AL
        pop ecx
        pop edi
        add edi, 320                    ; advance to next screen row
        dec ecx
        jnz .fill_row

        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

vga_get_cursor:
        ;; Output: DH = row (0..VGA_ROWS-1), DL = col (0..VGA_COLS-1).
        ;; Clobbers AX.  Preserves everything else.
        push bx
        push cx

        mov dx, VGA_CRTC_INDEX
        mov al, VGA_CRTC_CURSOR_HIGH
        out dx, al
        mov dx, VGA_CRTC_DATA
        in al, dx
        mov ch, al

        mov dx, VGA_CRTC_INDEX
        mov al, VGA_CRTC_CURSOR_LOW
        out dx, al
        mov dx, VGA_CRTC_DATA
        in al, dx
        mov cl, al

        mov ax, cx              ; AX = linear cursor position
        xor dx, dx
        mov bx, VGA_COLS
        div bx                  ; AX = row, DX = col (remainder)
        mov dh, al
        ;; DL already holds col from the divide remainder.

        pop cx
        pop bx
        ret

vga_scroll_up:
        ;; Scrolls the text buffer up by one row.  The top row is discarded;
        ;; the bottom row is cleared to space + default attribute.
        ;; Preserves all registers.
        push ax
        push cx
        push esi
        push edi

        mov esi, 0xB8000 + VGA_COLS * 2         ; source: row 1
        mov edi, 0xB8000                         ; dest:   row 0
        mov cx, (VGA_ROWS - 1) * VGA_COLS        ; word count
        cld
        rep movsw

        mov edi, 0xB8000 + (VGA_ROWS - 1) * VGA_COLS * 2
        mov ax, (VGA_DEFAULT_ATTRIBUTE << 8) | ' '
        mov cx, VGA_COLS
        rep stosw

        pop edi
        pop esi
        pop cx
        pop ax
        ret

vga_set_bg:
        ;; Input: AL = colour (0..15 for the standard palette).
        ;; Updates the attribute-controller overscan register.  Preserves
        ;; all registers.
        push ax
        push dx

        mov ah, al                      ; stash colour in AH

        mov dx, VGA_INPUT_STATUS_1
        in al, dx                       ; reset AC flip-flop

        mov dx, VGA_ATTR_WRITE
        mov al, VGA_AC_OVERSCAN | 20h   ; bit 5 = keep palette active
        out dx, al
        mov al, ah                      ; colour
        out dx, al

        pop dx
        pop ax
        ret

vga_set_cursor:
        ;; Input: DH = row, DL = col.  Clobbers AX.  Preserves everything else.
        push bx
        push cx
        push dx

        movzx ax, dh
        imul ax, ax, VGA_COLS           ; AX = row * 80 (DX not clobbered)
        movzx bx, dl                    ; BX = col (DX still valid)
        add ax, bx                      ; AX = row * 80 + col
        mov cx, ax                      ; CX = linear position

        mov dx, VGA_CRTC_INDEX
        mov al, VGA_CRTC_CURSOR_HIGH
        out dx, al
        mov dx, VGA_CRTC_DATA
        mov al, ch
        out dx, al

        mov dx, VGA_CRTC_INDEX
        mov al, VGA_CRTC_CURSOR_LOW
        out dx, al
        mov dx, VGA_CRTC_DATA
        mov al, cl
        out dx, al

        pop dx
        pop cx
        pop bx
        ret

vga_set_mode:
        ;; Input: AL = mode (VIDEO_MODE_TEXT_80x25=03h, VIDEO_MODE_VGA_320x200_256=13h).
        ;; Programs VGA registers from the mode table.  CF set if unsupported.
        ;; Preserves all registers.
        push eax
        push ebx
        push ecx
        push edx
        push esi
        push edi

        mov ah, al                      ; save requested mode

        mov esi, vga_mode_table
.find_mode:
        cmp esi, vga_mode_table_end
        jae .unsupported
        cmp byte [esi], ah
        je .found_mode
        add esi, VGA_MODE_ENTRY_SIZE
        jmp .find_mode

.unsupported:
        stc
        jmp .set_mode_done

.found_mode:
        inc esi                         ; skip mode-ID byte, ESI → Misc Output

        ;; 1. Miscellaneous Output
        mov dx, VGA_MISC_WRITE
        lodsb
        out dx, al

        ;; 2. Sequencer: hold in synchronous reset
        mov dx, VGA_SEQ_INDEX
        xor al, al
        out dx, al
        mov dx, VGA_SEQ_DATA
        mov al, 01h
        out dx, al

        ;; 3. Sequencer registers 1-4
        mov cx, 4
        mov bx, 1
.seq_loop:
        mov dx, VGA_SEQ_INDEX
        mov al, bl
        out dx, al
        mov dx, VGA_SEQ_DATA
        lodsb
        out dx, al
        inc bx
        dec cx
        jnz .seq_loop

        ;; 4. Release sequencer reset
        mov dx, VGA_SEQ_INDEX
        xor al, al
        out dx, al
        mov dx, VGA_SEQ_DATA
        mov al, 03h
        out dx, al

        ;; 5. Unlock CRTC registers 0-7 (clear protect bit in reg 11h)
        mov dx, VGA_CRTC_INDEX
        mov al, 11h
        out dx, al
        mov dx, VGA_CRTC_DATA
        in al, dx
        and al, 7Fh
        out dx, al

        ;; 6. CRTC registers 00h-18h (25 values)
        mov cx, 25
        xor bx, bx
.crtc_loop:
        mov dx, VGA_CRTC_INDEX
        mov al, bl
        out dx, al
        mov dx, VGA_CRTC_DATA
        lodsb
        out dx, al
        inc bx
        dec cx
        jnz .crtc_loop

        ;; 7. Graphics Controller registers 00h-08h (9 values)
        mov cx, 9
        xor bx, bx
.gc_loop:
        mov dx, VGA_GC_INDEX
        mov al, bl
        out dx, al
        mov dx, VGA_GC_DATA
        lodsb
        out dx, al
        inc bx
        dec cx
        jnz .gc_loop

        ;; 8. Attribute Controller registers 00h-14h (21 values)
        ;; Reset AC flip-flop to index state, then alternate index/data to 3C0h.
        mov dx, VGA_INPUT_STATUS_1
        in al, dx

        mov cx, 21
        xor bx, bx
.ac_loop:
        mov dx, VGA_ATTR_WRITE
        mov al, bl                      ; AC index (bit 5=0 → video blanked during programming)
        out dx, al
        lodsb
        out dx, al                      ; AC data
        inc bx
        dec cx
        jnz .ac_loop

        ;; 9. Re-enable screen output (PAS bit, bit 5 of AC index write)
        mov dx, VGA_ATTR_WRITE
        mov al, 20h
        out dx, al

        ;; 10. Restore default 16-colour DAC palette.  Done on every mode
        ;; switch (not just text mode) because BIOS mode 3 only populates
        ;; DAC entries 0-7, 20, and 56-63 for its text-mode AC palette;
        ;; mode 13h uses palette indices directly and would otherwise see
        ;; DAC[8..15] at whatever garbage BIOS left there (often a copy of
        ;; 0-7, which collapses draw's 14 trail colours into ~8 uniques).
        mov esi, vga_default_palette
        mov dx, 03C8h           ; DAC write-address port
        xor al, al
        out dx, al              ; start at palette entry 0
        inc dx                  ; 03C9h = DAC data (R then G then B per entry)
        mov cx, 16 * 3          ; 16 entries × 3 bytes
        cld
.dac_restore_loop:
        lodsb
        out dx, al
        dec cx
        jnz .dac_restore_loop

        ;; 11. Clear framebuffer using flat 32-bit addressing — DS / ES
        ;; already point to the protected mode flat data segment, so we just write
        ;; to the linear framebuffer address (0xB8000 for text, 0xA0000
        ;; for mode 13h).  No ES reload (a real-mode segment value would
        ;; #GP in protected mode).
        cmp ah, 13h
        je .clear_graphics
        ;; Text mode: fill 0xB8000 with space + default attribute
        mov edi, 0xB8000
        mov ax, (VGA_DEFAULT_ATTRIBUTE << 8) | ' '
        mov ecx, VGA_COLS * VGA_ROWS
        rep stosw
        jmp .clear_done
.clear_graphics:
        ;; Mode 13h: zero 320×200 = 64000 bytes at 0xA0000
        mov edi, 0xA0000
        xor eax, eax
        mov ecx, 320 * 200 / 2
        cld
        rep stosw
.clear_done:

        clc

.set_mode_done:
        pop edi
        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

vga_teletype:
        ;; Input: AL = character.  Uses [ansi_fg] as the attribute byte.
        ;; Handles CR (col←0), LF (row++ with scroll), BS (col--, no wrap).
        ;; Advances the cursor for regular characters, scrolling on overflow.
        ;; Preserves all registers.
        push eax
        push ebx
        push ecx
        push edx
        push edi

        cmp al, 0Dh
        je .cr
        cmp al, 0Ah
        je .lf
        cmp al, 08h
        je .bs

        ;; Normal char: write at cursor, then advance.
        mov cl, al                      ; stash char
        mov ch, [ansi_fg]               ; stash attribute
        call vga_get_cursor             ; DH=row, DL=col (clobbers AX)

        movzx eax, dh
        imul eax, eax, VGA_COLS         ; EAX = row * 80
        movzx ebx, dl                   ; EBX = col (DX still valid)
        add eax, ebx
        shl eax, 1                      ; byte offset
        add eax, 0xB8000                ; linear VGA address
        mov edi, eax

        mov al, cl                      ; char
        mov ah, ch                      ; attr
        mov [edi], ax

        inc dl
        cmp dl, VGA_COLS
        jb .set_cursor
        xor dl, dl
        inc dh
        cmp dh, VGA_ROWS
        jb .set_cursor
        call vga_scroll_up
        mov dh, VGA_ROWS - 1
.set_cursor:
        call vga_set_cursor
        jmp .done

.cr:
        call vga_get_cursor
        xor dl, dl
        call vga_set_cursor
        jmp .done

.lf:
        call vga_get_cursor
        inc dh
        cmp dh, VGA_ROWS
        jb .lf_set
        call vga_scroll_up
        mov dh, VGA_ROWS - 1
.lf_set:
        call vga_set_cursor
        jmp .done

.bs:
        call vga_get_cursor
        test dl, dl
        jz .done
        dec dl
        call vga_set_cursor
.done:
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

vga_write_attribute:
        ;; Input: AL = character, BL = attribute byte.  Writes at the current
        ;; cursor position with no advance and no scroll.  Preserves all
        ;; registers.
        push eax
        push ebx
        push ecx
        push edx
        push edi

        mov cl, al                      ; stash char
        mov ch, bl                      ; stash attr
        call vga_get_cursor             ; DH=row, DL=col (clobbers AX)

        movzx eax, dh
        imul eax, eax, VGA_COLS         ; EAX = row * 80
        movzx ebx, dl                   ; EBX = col (DX still valid)
        add eax, ebx
        shl eax, 1
        add eax, 0xB8000                ; linear VGA address
        mov edi, eax

        mov al, cl
        mov ah, ch
        mov [edi], ax

        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; fd_ioctl_vga: SYS_IO_IOCTL entry for /dev/vga fds.
;;; Input:  AL = cmd (VGA_IOCTL_*); args per cmd (see constants.asm).
;;; Output: CF = 1 on unsupported cmd or mode.
;;;
;;; Called via jmp from fd_ioctl, so returning with ret here goes back to
;;; the syscall handler's .iret_cf path.  BX was clobbered by the
;;; dispatch-table indirection; SI still points at the fd entry.
;;; -----------------------------------------------------------------------
fd_ioctl_vga:
        ;; Every VGA ioctl mutates device state (mode, framebuffer, DAC),
        ;; so the fd must have been opened O_WRONLY.  fd_lookup left ESI
        ;; pointing at the fd entry; the dispatch table jump into here
        ;; clobbered EBX but ESI survives.
        test byte [esi+FD_OFFSET_FLAGS], O_WRONLY
        jz .vga_bad
        cmp al, VGA_IOCTL_FILL_BLOCK
        je .vga_fill_block
        cmp al, VGA_IOCTL_MODE
        je .vga_mode
        cmp al, VGA_IOCTL_SET_PALETTE
        je .vga_set_palette
.vga_bad:
        stc
        ret

.vga_fill_block:
        ;; CL=col, CH=row, DL=color.  vga_fill_block's native ABI uses
        ;; BL/BH/AL, so shuffle before the call.
        mov bx, cx
        mov al, dl
        call vga_fill_block
        clc
        ret

.vga_mode:
        ;; DL = requested video mode.  Send CR+form-feed to serial, then
        ;; reprogram the VGA registers ONLY if the requested mode differs
        ;; from the currently-active mode.  Reprogramming on every call
        ;; flips SR03 (Character Map Select) and zeros the framebuffer,
        ;; which is wasteful for Ctrl+L (already-in-text → text) and risks
        ;; exposing font-load bugs unnecessarily.  On text-mode requests,
        ;; always finish with vga_clear_screen so Ctrl+L visibly clears.
        push ax
        mov al, `\r`
        call serial_character
        mov al, 0Ch
        call serial_character
        pop ax
        mov al, dl
        cmp al, [vga_current_mode]
        je .vga_mode_already
        call vga_set_mode       ; CF=1 on unsupported mode
        jc .vga_mode_done
        mov [vga_current_mode], al
.vga_mode_already:
        cmp al, VIDEO_MODE_TEXT_80x25
        jne .vga_mode_clear_done
        call vga_clear_screen
.vga_mode_clear_done:
        clc
.vga_mode_done:
        ret

.vga_set_palette:
        ;; CL=index, CH=r, DL=g, DH=b → pass straight through.
        call vga_set_palette_color
        clc
        ret

vga_set_palette_color:
        ;; Program DAC entry CL to (CH, DL, DH) in 6-bit R/G/B.
        ;; Writes index to 0x3C8 (write-address) then R/G/B sequentially
        ;; to 0x3C9 (data).  Preserves all registers.
        push ax
        push bx
        push dx
        mov bx, dx                      ; stash G (BL) and B (BH); DX freed
        mov dx, 03C8h
        mov al, cl                      ; index
        out dx, al
        inc dx                          ; 03C9h: R, G, B
        mov al, ch                      ; R
        out dx, al
        mov al, bl                      ; G
        out dx, al
        mov al, bh                      ; B
        out dx, al
        pop dx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; Data tables (kept at end of file, sorted alphabetically by label).
;;; -----------------------------------------------------------------------

;;; Currently-active video mode.  Initialised to mode 03h since the BIOS
;;; leaves us in 80×25 text after boot; vga_font_load runs against that
;;; state without programming our register table.  .vga_mode skips the
;;; full register reprogram (and SR03 flip) when the requested mode
;;; matches this value.
vga_current_mode db VIDEO_MODE_TEXT_80x25

;;; Default VGA 16-colour DAC palette (6-bit R, G, B per entry).
;;; Matches the standard BIOS palette for mode 03h; restored on every
;;; text-mode switch so graphics-mode programs can freely modify the DAC.
vga_default_palette:
        db  0,  0,  0   ;  0 black
        db  0,  0, 42   ;  1 dark blue
        db  0, 42,  0   ;  2 dark green
        db  0, 42, 42   ;  3 dark cyan
        db 42,  0,  0   ;  4 dark red
        db 42,  0, 42   ;  5 dark magenta
        db 42, 21,  0   ;  6 brown
        db 42, 42, 42   ;  7 light gray
        db 21, 21, 21   ;  8 dark gray
        db 21, 21, 63   ;  9 bright blue
        db 21, 63, 21   ; 10 bright green
        db 21, 63, 63   ; 11 bright cyan
        db 63, 21, 21   ; 12 bright red
        db 63, 21, 63   ; 13 bright magenta
        db 63, 63, 21   ; 14 yellow
        db 63, 63, 63   ; 15 white

;;; VGA mode register tables for vga_set_mode.
;;; Each entry: 1 mode-id + 1 misc + 4 seq(1-4) + 25 crtc(0-18h) + 9 gc(0-8) + 21 ac(0-14h)
vga_mode_table:

        ;; ----- Mode 03h: 80×25 16-colour text, 400 scan lines ---------------
        db 03h                          ; mode ID
        db 67h                          ; Miscellaneous Output
        ;; Sequencer regs 1-4
        db 00h                          ; Clocking Mode: 9-dot clocks
        db 03h                          ; Map Mask: planes 0+1
        db 05h                          ; Character Map Select: both char sets read font at plane 2 offset 0x4000 (populated by vga_font_load)
        db 02h                          ; Memory Mode: extended
        ;; CRTC regs 00h-18h (25 values)
        db 5Fh, 4Fh, 50h, 82h, 55h, 81h, 0BFh, 1Fh    ; 00-07
        db 00h, 4Fh, 0Dh, 0Eh, 00h, 00h, 00h, 00h     ; 08-0F
        db 9Ch, 8Eh, 8Fh, 28h, 1Fh, 96h, 0B9h, 0A3h   ; 10-17
        db 0FFh                                         ; 18
        ;; Graphics Controller regs 00h-08h (9 values)
        db 00h, 00h, 00h, 00h, 00h, 10h, 0Eh, 00h, 0FFh
        ;; Attribute Controller regs 00h-14h (21 values)
        db 00h, 01h, 02h, 03h, 04h, 05h, 14h, 07h      ; 00-07 palette
        db 38h, 39h, 3Ah, 3Bh, 3Ch, 3Dh, 3Eh, 3Fh     ; 08-0F palette
        db 0Ch, 00h, 0Fh, 08h, 00h                      ; 10-14 mode/overscan/planes/pan/select

        ;; ----- Mode 13h: 320×200 256-colour ---------------------------------
        db 13h                          ; mode ID
        db 63h                          ; Miscellaneous Output
        ;; Sequencer regs 1-4
        db 01h                          ; Clocking Mode: 8-dot clocks
        db 0Fh                          ; Map Mask: all 4 planes
        db 00h                          ; Character Map Select
        db 0Eh                          ; Memory Mode: chain4, extended
        ;; CRTC regs 00h-18h (25 values)
        db 5Fh, 4Fh, 50h, 82h, 54h, 80h, 0BFh, 1Fh    ; 00-07
        db 00h, 41h, 00h, 00h, 00h, 00h, 00h, 00h      ; 08-0F
        db 9Ch, 8Eh, 8Fh, 28h, 40h, 96h, 0B9h, 0A3h   ; 10-17
        db 0FFh                                         ; 18
        ;; Graphics Controller regs 00h-08h (9 values)
        db 00h, 00h, 00h, 00h, 00h, 40h, 05h, 0Fh, 0FFh
        ;; Attribute Controller regs 00h-14h (21 values)
        db 00h, 01h, 02h, 03h, 04h, 05h, 06h, 07h      ; 00-07 palette
        db 08h, 09h, 0Ah, 0Bh, 0Ch, 0Dh, 0Eh, 0Fh     ; 08-0F palette
        db 41h, 00h, 0Fh, 00h, 00h                      ; 10-14 mode/overscan/planes/pan/select

vga_mode_table_end:
