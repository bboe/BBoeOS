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
        VGA_CRTC_INDEX          equ 03D4h
        VGA_CRTC_DATA           equ 03D5h
        VGA_CRTC_CURSOR_HIGH    equ 0Eh
        VGA_CRTC_CURSOR_LOW     equ 0Fh
        VGA_ATTR_WRITE          equ 03C0h
        VGA_INPUT_STATUS_1      equ 03DAh
        VGA_AC_OVERSCAN         equ 11h
        VGA_DEFAULT_ATTRIBUTE   equ 07h

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
        mov bx, VGA_COLS
        mul bx                          ; AX = row * 80 (DX clobbered, stacked copy preserved)
        movzx bx, dl
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
        mov bx, VGA_COLS
        mul bx                          ; AX = row*80
        movzx bx, dl
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
        mov bx, VGA_COLS
        mul bx
        movzx bx, dl
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
