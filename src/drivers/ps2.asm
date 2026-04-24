;;; ------------------------------------------------------------------------
;;; ps2.asm — native PS/2 keyboard driver.
;;;
;;; Replaces INT 16h AH=00h and AH=01h for fd.asm's console-read path by
;;; polling the 8042 directly: port 0x60 is the data register, port 0x64
;;; carries the status byte (bit 0 = output-buffer full).
;;;
;;; Translates Set 1 scan codes to ASCII via a small unshifted / shifted
;;; keymap pair.  Tracks shift and ctrl state across make/break pairs, and
;;; decodes the 0xE0 prefix just for the cursor-pad arrows.  A one-slot
;;; buffer holds a decoded key so ps2_check can peek without consuming.
;;;
;;; Surface (both return BIOS-compatible AL = ASCII / AH = scan code):
;;;     ps2_init        - mask IRQ 1 at the master PIC so the BIOS IRQ
;;;                       handler stops draining port 0x60 behind us.
;;;                       Zeros the driver state.  Call once, early.
;;;     ps2_check       - ZF=0 if a decoded key is ready, ZF=1 otherwise.
;;;                       Non-blocking; does not consume the buffered key.
;;;     ps2_read        - blocks until a key is ready, returns AL = ASCII
;;;                       (0 for extended arrows) and AH = scan code.
;;; ------------------------------------------------------------------------

        PS2_DATA               equ 60h
        PS2_STATUS             equ 64h
        PS2_STATUS_OUTPUT_FULL equ 01h
        PIC1_DATA              equ 21h
        PIC_IRQ1_MASK          equ 02h

ps2_check:
        ;; Drain any pending scan codes into the buffer, then report
        ;; whether a decoded key landed there.  Preserves all registers.
        push ax
        call ps2_service
        cmp byte [ps2_buffered], 0
        pop ax
        ret

ps2_init:
        ;; Mask IRQ 1 at the master 8259 so BIOS's keyboard IRQ handler
        ;; stops racing us for port 0x60, and clear the driver state.
        push ax
        in al, PIC1_DATA
        or al, PIC_IRQ1_MASK
        out PIC1_DATA, al
        xor al, al
        mov [ps2_buffered], al
        mov [ps2_buffered_al], al
        mov [ps2_buffered_ah], al
        mov [ps2_extended], al
        mov [ps2_shift], al
        mov [ps2_ctrl], al
        pop ax
        ret

ps2_read:
        ;; Block until a decoded key is ready, then return it.
        ;; Output: AL = ASCII (0 for extended arrows), AH = scan code.
        .wait:
        call ps2_service
        cmp byte [ps2_buffered], 0
        je .wait
        mov al, [ps2_buffered_al]
        mov ah, [ps2_buffered_ah]
        mov byte [ps2_buffered], 0
        ret

ps2_read_scancode:
        ;; Output: CF=0 and AL = scancode if a byte is pending, CF=1 if
        ;; the 8042's output buffer is empty.  Clobbers AX.
        in al, PS2_STATUS
        test al, PS2_STATUS_OUTPUT_FULL
        jz .empty
        in al, PS2_DATA
        clc
        ret
        .empty:
        stc
        ret

ps2_service:
        ;; Consume any pending scan codes from the 8042, updating modifier
        ;; state and populating the decoded-key buffer if a regular key
        ;; (or one of the tracked extended arrows) arrives.  Returns when
        ;; either the buffer is filled or the hardware queue is empty.
        ;; Preserves all registers.
        push ax
        push bx
        .loop:
        cmp byte [ps2_buffered], 0
        jne .done               ; a key is already ready; stop
        call ps2_read_scancode
        jc .done                ; hardware queue drained
        cmp al, 0E0h
        jne .not_prefix
        mov byte [ps2_extended], 1
        jmp .loop
        .not_prefix:
        mov bl, al
        and bl, 7Fh             ; BL = scan code with release bit stripped
        test al, 80h
        jnz .release

        ;; Make (key press).
        cmp bl, 2Ah             ; LShift
        je .press_shift
        cmp bl, 36h             ; RShift
        je .press_shift
        cmp bl, 1Dh             ; Ctrl (both sides)
        je .press_ctrl
        cmp byte [ps2_extended], 0
        jne .extended
        ;; Regular key.
        cmp bl, 3Bh
        jae .discard            ; above what we map
        movzx bx, bl
        cmp byte [ps2_shift], 0
        jne .shifted
        mov al, [ps2_map_unshift + bx]
        jmp .have_ascii
        .shifted:
        mov al, [ps2_map_shift + bx]
        .have_ascii:
        test al, al
        jz .discard
        cmp byte [ps2_ctrl], 0
        je .store
        ;; Ctrl+letter → control code (1..26).  Non-letters pass through.
        mov ah, al
        and ah, 5Fh             ; uppercase if alpha
        cmp ah, 'A'
        jb .store
        cmp ah, 'Z'
        ja .store
        sub ah, 'A' - 1
        mov al, ah
        .store:
        mov [ps2_buffered_al], al
        mov byte [ps2_buffered_ah], 0
        mov byte [ps2_buffered], 1
        jmp .discard

        .extended:
        ;; Extended prefix set.  Only cursor-pad arrows produce output,
        ;; matching BIOS INT 16h AH=00 semantics (AL=0, AH=scan code).
        cmp bl, 48h             ; UP
        je .arrow
        cmp bl, 50h             ; DOWN
        je .arrow
        cmp bl, 4Dh             ; RIGHT
        je .arrow
        cmp bl, 4Bh             ; LEFT
        je .arrow
        jmp .discard
        .arrow:
        mov byte [ps2_buffered_al], 0
        mov [ps2_buffered_ah], bl
        mov byte [ps2_buffered], 1
        jmp .discard

        .press_shift:
        mov byte [ps2_shift], 1
        jmp .discard
        .press_ctrl:
        mov byte [ps2_ctrl], 1
        jmp .discard

        .release:
        ;; Break (key release).  Only modifier releases matter.
        cmp bl, 2Ah
        je .release_shift
        cmp bl, 36h
        je .release_shift
        cmp bl, 1Dh
        je .release_ctrl
        jmp .discard
        .release_shift:
        mov byte [ps2_shift], 0
        jmp .discard
        .release_ctrl:
        mov byte [ps2_ctrl], 0

        .discard:
        ;; Any prefix applied only to the scan code we just processed.
        mov byte [ps2_extended], 0
        jmp .loop
        .done:
        pop bx
        pop ax
        ret

        ;; Set-1 scan code → ASCII.  Index 0 is a dummy; scan codes beyond
        ;; 0x3A are function keys / CapsLock and are rejected before the
        ;; table is consulted.
ps2_map_unshift:
        db 0, 1Bh                               ; 00 unused, 01 ESC
        db '1','2','3','4','5','6','7','8','9','0'
        db '-','='
        db 08h, 09h                             ; BS, TAB
        db 'q','w','e','r','t','y','u','i','o','p'
        db '[',']'
        db 0Dh                                  ; Enter
        db 0                                    ; LCtrl
        db 'a','s','d','f','g','h','j','k','l'
        db ';',27h,'`'                          ; 27h = apostrophe
        db 0                                    ; LShift
        db '\'
        db 'z','x','c','v','b','n','m'
        db ',','.','/'
        db 0                                    ; RShift
        db '*'                                  ; keypad *
        db 0                                    ; LAlt
        db ' '
        db 0                                    ; CapsLock

ps2_map_shift:
        db 0, 1Bh
        db '!','@','#','$','%','^','&','*','(',')'
        db '_','+'
        db 08h, 09h
        db 'Q','W','E','R','T','Y','U','I','O','P'
        db '{','}'
        db 0Dh
        db 0
        db 'A','S','D','F','G','H','J','K','L'
        db ':','"','~'
        db 0
        db '|'
        db 'Z','X','C','V','B','N','M'
        db '<','>','?'
        db 0
        db '*'
        db 0
        db ' '
        db 0

        ;; Driver state
        ps2_buffered    db 0
        ps2_buffered_al db 0
        ps2_buffered_ah db 0
        ps2_extended    db 0
        ps2_shift       db 0
        ps2_ctrl        db 0
