;;; ------------------------------------------------------------------------
;;; vga_font.asm — boot-time VGA font loader (real mode only).
;;;
;;; Copies the BIOS ROM 8x16 font into char-gen at plane 2 offset 0x4000
;;; before the pmode flip.  The mode-03h table in drivers/vga.asm sets
;;; SR03=05h, which selects this slot for both character maps; without
;;; this loader, switching back to text mode after a graphics mode (e.g.
;;; via `draw`) leaves the character generator pointed at empty VRAM and
;;; the screen renders as blank glyphs.
;;;
;;; Must run while BIOS is still mapped: uses INT 10h AH=11h AL=30h to
;;; query the ROM font pointer.  Slot 0x4000 (rather than 0x0000) keeps
;;; the font safe across mode-13h enters — mode 13h's framebuffer clear
;;; zeroes plane 2 bytes 0..15999, which would otherwise corrupt the
;;; default character-map slot.
;;;
;;; Every GC register the planar write path consults is initialised
;;; explicitly: if GR01 (enable set/reset) is left non-zero for plane 2,
;;; or GR03 non-zero (data rotate / logic op), the copy silently writes
;;; a uniform pattern from GR00 instead of CPU data and every glyph
;;; ends up as a solid block.
;;; ------------------------------------------------------------------------

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

        ;; Fetch ROM 8x16 font pointer -> ES:BP (CX = 16 pts/char).
        mov ax, 1130h
        mov bh, 06h
        int 10h
        ;; Move ROM pointer into DS:SI for the copy loop.
        push es
        pop ds
        mov si, bp

        ;; Configure planar write path: plane 2 only, A000h window.
        mov dx, VGA_SEQ_INDEX
        mov al, 04h
        out dx, al
        mov dx, VGA_SEQ_DATA
        mov al, 06h                     ; SR04: extended, no chain-4, no odd/even
        out dx, al

        mov dx, VGA_SEQ_INDEX
        mov al, 02h
        out dx, al
        mov dx, VGA_SEQ_DATA
        mov al, 04h                     ; SR02: write plane 2 only
        out dx, al

        mov dx, VGA_GC_INDEX
        mov al, 00h
        out dx, al
        mov dx, VGA_GC_DATA
        xor al, al                      ; GR00: set/reset data = 0
        out dx, al

        mov dx, VGA_GC_INDEX
        mov al, 01h
        out dx, al
        mov dx, VGA_GC_DATA
        xor al, al                      ; GR01: disable set/reset on all planes
        out dx, al

        mov dx, VGA_GC_INDEX
        mov al, 03h
        out dx, al
        mov dx, VGA_GC_DATA
        xor al, al                      ; GR03: no rotate, replace (no ALU op)
        out dx, al

        mov dx, VGA_GC_INDEX
        mov al, 05h
        out dx, al
        mov dx, VGA_GC_DATA
        xor al, al                      ; GR05: write mode 0
        out dx, al

        mov dx, VGA_GC_INDEX
        mov al, 06h
        out dx, al
        mov dx, VGA_GC_DATA
        mov al, 05h                     ; GR06: A000h base, 64KB, graphics mode
        out dx, al

        mov dx, VGA_GC_INDEX
        mov al, 08h
        out dx, al
        mov dx, VGA_GC_DATA
        mov al, 0FFh                    ; GR08: bit mask = all bits writable
        out dx, al

        ;; Copy 256 glyphs * (16 bytes bitmap + 16 bytes zero padding) from
        ;; DS:SI (ROM) to ES:DI (VRAM plane 2 offset 0x4000).  The VGA
        ;; char-gen uses fixed 32-byte slots regardless of glyph height.
        mov ax, 0A000h                  ; VGA graphics segment (real-mode value)
        mov es, ax
        mov di, 4000h
        mov bx, 256                     ; glyph count
        cld
.char_loop:
        mov cx, 8                       ; 16 bytes / 2 = 8 words of bitmap
        rep movsw
        mov cx, 8                       ; 16 bytes of zero padding
        xor ax, ax
        rep stosw
        dec bx
        jnz .char_loop

        ;; Restore text-mode planar state (matches mode 03h defaults).
        mov dx, VGA_SEQ_INDEX
        mov al, 04h
        out dx, al
        mov dx, VGA_SEQ_DATA
        mov al, 02h                     ; SR04: extended, odd/even
        out dx, al

        mov dx, VGA_SEQ_INDEX
        mov al, 02h
        out dx, al
        mov dx, VGA_SEQ_DATA
        mov al, 03h                     ; SR02: planes 0+1
        out dx, al

        mov dx, VGA_GC_INDEX
        mov al, 05h
        out dx, al
        mov dx, VGA_GC_DATA
        mov al, 10h                     ; GR05: odd/even read enabled
        out dx, al

        mov dx, VGA_GC_INDEX
        mov al, 06h
        out dx, al
        mov dx, VGA_GC_DATA
        mov al, 0Eh                     ; GR06: B800h 32KB, odd/even, text
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
