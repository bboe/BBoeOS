;;; ------------------------------------------------------------------------
;;; ps2.asm — PS/2 keyboard driver (32-bit protected mode).
;;;
;;; IRQ-driven.  pmode_irq1_handler (local to ps2_init) reads the raw
;;; scancode from port 0x60 and calls ps2_handle_scancode, which tracks modifier
;;; state (shift, ctrl, 0xE0 extended prefix), translates Set-1 make
;;; codes to ASCII via ps2_map_unshift / ps2_map_shift, and queues the
;;; result in a 16-byte ring buffer.
;;;
;;; Surface:
;;;   ps2_getc   Non-blocking read.  AL = ASCII char, or AL=0 (ZF set) if empty.
;;;   ps2_init   Install IRQ 1 handler and unmask IRQ 1.  Call once before sti.
;;;
;;; Not supported (previously provided transparently by BIOS INT 16h):
;;;   Caps Lock   — scancode 0x3A is in the table but toggle state is not tracked.
;;;   Alt         — scancode 0x38 is in the table but modifier state is not tracked.
;;;   F-keys      — scancodes 0x3B–0x44 are above the table boundary and discarded.
;;;   Extended keys beyond arrows (Home/End/PgUp/PgDn/Insert/Delete) — discarded.
;;; ------------------------------------------------------------------------

        KB_BUFFER_SIZE          equ 16          ; must be a power of 2
        PMODE_PIC1_CMD          equ 20h
        PMODE_PIC1_DATA         equ 21h
        PMODE_PIC_EOI           equ 20h
        PMODE_IRQ1_VECTOR       equ 21h
        PS2_DATA                equ 60h

ps2_getc:
        ;; Non-blocking read from the ring buffer.
        ;; Returns AL = ASCII char, or AL=0 (ZF set) if buffer is empty.
        movzx eax, byte [ps2_head]
        cmp al, [ps2_tail]
        je .empty
        movzx ecx, byte [ps2_head]
        mov al, [ps2_buf + ecx]
        inc cl
        and cl, KB_BUFFER_SIZE - 1
        mov [ps2_head], cl
        ret
        .empty:
        xor al, al
        ret

ps2_handle_scancode:
        ;; AL = raw scancode from port 0x60.  Tracks shift/ctrl/extended
        ;; state and pushes translated ASCII to the ring buffer.
        push eax
        push ecx
        push edx

        ;; Shift and ctrl break codes arrive with bit 7 set.
        cmp al, 0AAh                    ; LShift release
        je .shift_clear
        cmp al, 0B6h                    ; RShift release
        je .shift_clear
        cmp al, 09Dh                    ; Ctrl release (0x1D | 0x80)
        je .ctrl_clear

        test al, 80h                    ; ignore all other break codes
        jnz .discard

        cmp al, 0E0h                    ; extended-key prefix
        je .set_extended

        cmp al, 2Ah                     ; LShift press
        je .shift_set
        cmp al, 36h                     ; RShift press
        je .shift_set
        cmp al, 1Dh                     ; Ctrl press
        je .ctrl_set

        cmp byte [ps2_extended], 0
        jne .handle_extended

        ;; Regular key: translate via unshift or shift table.
        cmp al, 3Bh                     ; F-keys (0x3B–0x44) and above — not supported
        jae .discard
        movzx ecx, al
        cmp byte [ps2_shift], 0
        jne .shifted
        mov al, [ps2_map_unshift + ecx]
        jmp .have_ascii
        .shifted:
        mov al, [ps2_map_shift + ecx]
        .have_ascii:
        test al, al
        jz .discard

        ;; Ctrl+letter → control code (^A=1 … ^Z=26).
        cmp byte [ps2_ctrl], 0
        je .push
        mov ah, al
        and ah, 5Fh                     ; force uppercase
        cmp ah, 'A'
        jb .push
        cmp ah, 'Z'
        ja .push
        sub ah, 'A' - 1
        mov al, ah
        .push:
        call ps2_putc
        jmp .discard

        .handle_extended:
        ;; Arrow keys (0xE0 prefix): emit ANSI CSI sequences ESC[A–D.
        ;; Up=48h Left=4Bh Right=4Dh Down=50h.
        ;; Other 0xE0 extended keys (Home/End/PgUp/PgDn/Insert/Delete) not supported.
        cmp al, 48h
        je .arrow_up
        cmp al, 4Bh
        je .arrow_left
        cmp al, 4Dh
        je .arrow_right
        cmp al, 50h
        je .arrow_down
        jmp .discard

        .arrow_down:  mov al, 1Bh; call ps2_putc; mov al, '['; call ps2_putc; mov al, 'B'; call ps2_putc; jmp .discard
        .arrow_left:  mov al, 1Bh; call ps2_putc; mov al, '['; call ps2_putc; mov al, 'D'; call ps2_putc; jmp .discard
        .arrow_right: mov al, 1Bh; call ps2_putc; mov al, '['; call ps2_putc; mov al, 'C'; call ps2_putc; jmp .discard
        .arrow_up:    mov al, 1Bh; call ps2_putc; mov al, '['; call ps2_putc; mov al, 'A'; call ps2_putc; jmp .discard

        .shift_set:
        mov byte [ps2_shift], 1
        jmp .discard
        .ctrl_set:
        mov byte [ps2_ctrl], 1
        jmp .discard
        .set_extended:
        mov byte [ps2_extended], 1
        jmp .done                       ; do NOT clear extended flag yet

        .shift_clear:
        mov byte [ps2_shift], 0
        jmp .discard
        .ctrl_clear:
        mov byte [ps2_ctrl], 0
        .discard:
        mov byte [ps2_extended], 0
        .done:
        pop edx
        pop ecx
        pop eax
        ret

ps2_init:
        ;; Install pmode_irq1_handler at IDT vector 0x21 and unmask IRQ 1.
        ;; Call once from entry.asm before sti.  Preserves all registers.
        push eax
        push ebx
        mov eax, .pmode_irq1_handler
        mov bl, PMODE_IRQ1_VECTOR
        call idt_set_gate32
        in al, PMODE_PIC1_DATA
        and al, 0FDh                    ; clear bit 1 (unmask IRQ 1)
        out PMODE_PIC1_DATA, al
        pop ebx
        pop eax
        ret

        .pmode_irq1_handler:
        ;; IRQ 1 (PS/2 keyboard).  Read raw scancode, delegate to
        ;; ps2_handle_scancode for translation and buffering, then EOI.
        push eax
        in al, PS2_DATA
        call ps2_handle_scancode
        mov al, PMODE_PIC_EOI
        out PMODE_PIC1_CMD, al
        pop eax
        iretd

ps2_putc:
        ;; Push AL into the ring buffer.  Called from ps2_handle_scancode
        ;; (IRQ context, interrupts off).  Drops character silently if full.
        push ecx
        push edx
        movzx ecx, byte [ps2_tail]
        lea edx, [ecx + 1]
        and dl, KB_BUFFER_SIZE - 1
        cmp dl, [ps2_head]              ; full when next tail == head
        je .full
        mov [ps2_buf + ecx], al
        mov [ps2_tail], dl
        .full:
        pop edx
        pop ecx
        ret

;;; Ring buffer (single-producer IRQ / single-consumer main loop).
ps2_buf   times KB_BUFFER_SIZE db 0
ps2_head  db 0
ps2_tail  db 0

;;; Modifier state.
ps2_ctrl     db 0
ps2_extended db 0
ps2_shift    db 0

;;; Set-1 scan code → ASCII translation tables.
ps2_map_shift:                                  ; shifted; same layout as unshifted
        db 0, 1Bh
        db '!','@','#','$','%','^','&','*','(',')'
        db '_','+'
        db 08h, 09h
        db 'Q','W','E','R','T','Y','U','I','O','P'
        db '{','}'
        db 0Dh
        db 0
        db 'A','S','D','F','G','H','J','K','L'
        db ':', '"', '~'
        db 0
        db '|'
        db 'Z','X','C','V','B','N','M'
        db '<','>','?'
        db 0
        db '*'
        db 0
        db ' '
        db 0
ps2_map_unshift:                                ; unshifted; codes 0x00–0x3A only
        db 0, 1Bh                               ; 00 unused, 01 ESC
        db '1','2','3','4','5','6','7','8','9','0'
        db '-','='
        db 08h, 09h                             ; BS, TAB
        db 'q','w','e','r','t','y','u','i','o','p'
        db '[',']'
        db 0Dh                                  ; Enter
        db 0                                    ; LCtrl
        db 'a','s','d','f','g','h','j','k','l'
        db ';', 27h, '`'                        ; 27h = apostrophe
        db 0                                    ; LShift
        db '\'
        db 'z','x','c','v','b','n','m'
        db ',','.','/'
        db 0                                    ; RShift
        db '*'                                  ; keypad *
        db 0                                    ; LAlt (modifier not tracked)
        db ' '
        db 0                                    ; CapsLock (toggle not tracked)
