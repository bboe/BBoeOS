;;; ------------------------------------------------------------------------
;;; vga.asm — native VGA text-mode driver.
;;;
;;; Replaces the INT 10h subroutines the ANSI parser (ansi.asm) and the
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

        VGA_COLS                equ 80
        VGA_ROWS                equ 25
        VGA_SEG                 equ 0B800h
        VGA_SEG_GRAPHICS        equ 0A000h
        VGA_MISC_WRITE          equ 03C2h
        VGA_SEQ_INDEX           equ 03C4h
        VGA_SEQ_DATA            equ 03C5h
        VGA_CRTC_INDEX          equ 03D4h
        VGA_CRTC_DATA           equ 03D5h
        VGA_CRTC_CURSOR_HIGH    equ 0Eh
        VGA_CRTC_CURSOR_LOW     equ 0Fh
        VGA_GC_INDEX            equ 03CEh
        VGA_GC_DATA             equ 03CFh
        VGA_ATTR_WRITE          equ 03C0h
        VGA_INPUT_STATUS_1      equ 03DAh
        VGA_AC_OVERSCAN         equ 11h
        VGA_DEFAULT_ATTRIBUTE   equ 07h
        VGA_MODE_ENTRY_SIZE     equ 61  ; 1 mode-id + 1 misc + 4 seq + 25 crtc + 9 gc + 21 ac

vga_fill_block:
        ;; Fill an 8×8 tile at (BL=col, BH=row) with color AL in mode 13h.
        ;; Tile coordinates: 0-39 columns, 0-24 rows.  Preserves all registers.
        push ax
        push bx
        push cx
        push dx
        push di
        push es

        mov cl, al                      ; stash color

        ;; DI = row * 2560 + col * 8  (pixel offset of tile's top-left corner)
        movzx ax, bh                    ; AX = row
        mov dx, 2560
        mul dx                          ; AX = row × 2560 (max 24×2560=61440, no overflow)
        mov di, ax

        movzx ax, bl                    ; AX = col
        mov dx, 8
        mul dx                          ; AX = col × 8 (max 39×8=312, no overflow)
        add di, ax

        mov ax, VGA_SEG_GRAPHICS
        mov es, ax
        mov al, cl                      ; restore color

        mov cx, 8                       ; 8 tile rows
        cld
.fill_row:
        push di
        push cx
        mov cx, 8
        rep stosb                       ; write 8 pixels with color AL
        pop cx
        pop di
        add di, 320                     ; advance to next screen row
        dec cx
        jnz .fill_row

        pop es
        pop di
        pop dx
        pop cx
        pop bx
        pop ax
        ret

vga_clear_screen:
        ;; Fills the text buffer with space + default attribute and moves
        ;; the cursor to (0,0).  Preserves all registers.
        push ax
        push cx
        push dx
        push di
        push es

        mov ax, VGA_SEG
        mov es, ax
        xor di, di
        mov ax, (VGA_DEFAULT_ATTRIBUTE << 8) | ' '
        mov cx, VGA_COLS * VGA_ROWS
        cld
        rep stosw

        xor dx, dx              ; DH=0 row, DL=0 col
        call vga_set_cursor

        pop es
        pop di
        pop dx
        pop cx
        pop ax
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
        push si
        push di
        push ds
        push es

        mov ax, VGA_SEG
        mov ds, ax
        mov es, ax

        mov si, VGA_COLS * 2                    ; source: row 1
        xor di, di                              ; dest:   row 0
        mov cx, (VGA_ROWS - 1) * VGA_COLS       ; word count
        cld
        rep movsw

        mov di, (VGA_ROWS - 1) * VGA_COLS * 2
        mov ax, (VGA_DEFAULT_ATTRIBUTE << 8) | ' '
        mov cx, VGA_COLS
        rep stosw

        pop es
        pop ds
        pop di
        pop si
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
        push ax
        push bx
        push cx
        push dx
        push si

        mov ah, al                      ; save requested mode

        mov si, vga_mode_table
.find_mode:
        cmp si, vga_mode_table_end
        jae .unsupported
        cmp byte [si], ah
        je .found_mode
        add si, VGA_MODE_ENTRY_SIZE
        jmp .find_mode

.unsupported:
        stc
        jmp .set_mode_done

.found_mode:
        inc si                          ; skip mode-ID byte, SI → Misc Output

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

        clc

.set_mode_done:
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        ret

vga_teletype:
        ;; Input: AL = character.  Uses [ansi_fg] as the attribute byte.
        ;; Handles CR (col←0), LF (row++ with scroll), BS (col--, no wrap).
        ;; Advances the cursor for regular characters, scrolling on overflow.
        ;; Preserves all registers.
        push ax
        push bx
        push cx
        push dx
        push di
        push es

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

        movzx ax, dh
        imul ax, ax, VGA_COLS           ; AX = row * 80 (DX not clobbered)
        movzx bx, dl                    ; BX = col (DX still valid)
        add ax, bx
        shl ax, 1                       ; byte offset
        mov di, ax

        mov ax, VGA_SEG
        mov es, ax
        mov al, cl                      ; char
        mov ah, ch                      ; attr
        mov [es:di], ax

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
        pop es
        pop di
        pop dx
        pop cx
        pop bx
        pop ax
        ret

vga_write_attribute:
        ;; Input: AL = character, BL = attribute byte.  Writes at the current
        ;; cursor position with no advance and no scroll.  Preserves all
        ;; registers.
        push ax
        push bx
        push cx
        push dx
        push di
        push es

        mov cl, al                      ; stash char
        mov ch, bl                      ; stash attr
        call vga_get_cursor             ; DH=row, DL=col (clobbers AX)

        movzx ax, dh
        imul ax, ax, VGA_COLS           ; AX = row * 80 (DX not clobbered)
        movzx bx, dl                    ; BX = col (DX still valid)
        add ax, bx
        shl ax, 1
        mov di, ax

        mov ax, VGA_SEG
        mov es, ax
        mov al, cl
        mov ah, ch
        mov [es:di], ax

        pop es
        pop di
        pop dx
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; VGA mode register tables for vga_set_mode.
;;; Each entry: 1 mode-id + 1 misc + 4 seq(1-4) + 25 crtc(0-18h) + 9 gc(0-8) + 21 ac(0-14h)
;;; -----------------------------------------------------------------------

vga_mode_table:

        ;; ----- Mode 03h: 80×25 16-colour text, 400 scan lines ---------------
        db 03h                          ; mode ID
        db 67h                          ; Miscellaneous Output
        ;; Sequencer regs 1-4
        db 00h                          ; Clocking Mode: 9-dot clocks
        db 03h                          ; Map Mask: planes 0+1
        db 00h                          ; Character Map Select
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
