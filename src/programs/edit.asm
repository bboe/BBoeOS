        org 6000h

%include "constants.asm"

        ;; Gap buffer in memory [BUF_BASE .. BUF_BASE+BUF_SIZE):
        ;;   [0 .. gap_start)        text before cursor  (logical offsets 0..gap_start-1)
        ;;   [gap_start .. gap_end)  gap (free space)
        ;;   [gap_end .. BUF_SIZE)   text after cursor   (logical offsets gap_start..len-1)
        %assign BUF_BASE      0600h   ; start of gap buffer (just above input buffer at 0x500)
        %assign BUF_SIZE      5000h   ; 20KB
        %assign KILL_BUF      5600h   ; kill buffer: free region between gap buf and program (0x5600–0x5FFF)
        %assign KILL_BUF_SIZE 0A00h   ; 2560 bytes

        ;; Screen layout: rows 0–23 for text, row 24 for status bar
        %assign EDIT_ROWS 24
        %assign EDIT_COLS 80

main:
        cld

        ;; Require a filename argument
        mov bx, [EXEC_ARG]
        test bx, bx
        jz .usage
        mov [filename], bx

        ;; Try to find the file in the directory
        mov si, bx
        mov ah, SYS_FS_FIND
        int 30h
        jc .new_file_err       ; file not found -- can't create new files yet

        ;; Record original on-disk size and start sector
        mov ax, [bx+14]
        mov [orig_size], ax
        mov al, [bx+12]
        mov [file_sector], al

        ;; Load file content into gap buffer: text goes AFTER the gap so
        ;; gap_start=0 and cursor_line/col=0 are consistent (cursor at start).
        ;; gap_start = 0, gap_end = BUF_SIZE - orig_size
        mov word [gap_start], 0
        mov ax, BUF_SIZE
        sub ax, [orig_size]
        mov [gap_end], ax

        ;; Read sectors into BUF_BASE + gap_end
        mov al, [file_sector]
        mov di, BUF_BASE
        add di, [gap_end]      ; DI = first byte of content area
        xor dx, dx             ; DX = bytes loaded so far
        .load_loop:
        mov ah, SYS_FS_READ
        int 30h
        jc .load_err
        ;; Copy only the valid bytes for this sector (avoid overrunning buffer)
        push si
        push cx
        cld
        mov si, DISK_BUFFER
        mov cx, [orig_size]
        sub cx, dx             ; CX = bytes remaining
        cmp cx, 512
        jbe .copy_sector
        mov cx, 512
        .copy_sector:
        rep movsb
        pop cx
        pop si
        inc al
        add dx, 512
        cmp dx, [orig_size]
        jb .load_loop

        ;; Set cursor to start of file
        mov word [cursor_col], 0
        mov word [cursor_line], 0
        mov word [view_line], 0
        mov byte [dirty], 0

        .editor_loop:
        call render
        call get_input
        jmp .editor_loop

        .new_file_err:
        mov si, MSG_NEW_FILE_ERR
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .load_err:
        mov si, MSG_LOAD_ERR
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .usage:
        mov si, MSG_USAGE
        mov ah, SYS_IO_PUTS
        int 30h
        mov ah, SYS_EXIT
        int 30h

;;; -----------------------------------------------------------------------
;;; buf_char_at: get logical char at offset BX
;;; Returns AL = char, CF set if BX >= logical length
;;; Preserves all registers except AL and flags
;;; -----------------------------------------------------------------------
buf_char_at:
        push bx
        push si
        ;; Compute logical length into SI
        mov si, [gap_end]
        sub si, [gap_start]    ; SI = gap size
        neg si
        add si, BUF_SIZE       ; SI = logical length
        cmp bx, si
        jae .past_end
        ;; Map logical offset BX to raw index
        cmp bx, [gap_start]
        jb .before_gap
        mov si, [gap_end]
        sub si, [gap_start]
        add bx, si             ; raw index = BX + gap_size
        .before_gap:
        mov al, [BUF_BASE + bx]
        clc
        pop si
        pop bx
        ret
        .past_end:
        stc
        pop si
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; buf_delete_after: delete char after cursor (Delete key)
;;; -----------------------------------------------------------------------
buf_delete_after:
        push bx
        mov bx, [gap_end]
        cmp bx, BUF_SIZE
        jae .done              ; nothing after cursor
        inc word [gap_end]
        mov byte [dirty], 1
        .done:
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; buf_delete_before: delete char before cursor (Backspace)
;;; -----------------------------------------------------------------------
buf_delete_before:
        push ax
        push bx
        cmp word [gap_start], 0
        je .done
        mov bx, [gap_start]
        dec bx
        mov al, [BUF_BASE + bx]
        dec word [gap_start]
        mov byte [dirty], 1
        cmp al, 0Ah
        je .was_newline
        cmp word [cursor_col], 0
        je .done
        dec word [cursor_col]
        jmp .done
        .was_newline:
        cmp word [cursor_line], 0
        je .done
        dec word [cursor_line]
        call recompute_col
        call check_scroll_up
        .done:
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; buf_insert: insert AL at cursor
;;; -----------------------------------------------------------------------
buf_insert:
        push ax
        push bx
        mov bx, [gap_start]
        cmp bx, [gap_end]
        je .full               ; buffer full
        mov [BUF_BASE + bx], al
        inc word [gap_start]
        mov byte [dirty], 1
        cmp al, 0Ah
        je .newline
        inc word [cursor_col]
        jmp .done
        .newline:
        inc word [cursor_line]
        mov word [cursor_col], 0
        call check_scroll
        .done:
        pop bx
        pop ax
        ret
        .full:
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; buf_length: logical text length into AX
;;; -----------------------------------------------------------------------
buf_length:
        push bx
        mov ax, BUF_SIZE
        mov bx, [gap_end]
        sub bx, [gap_start]
        sub ax, bx
        pop bx
        ret

;;; -----------------------------------------------------------------------
;;; check_hscroll: adjust view_col so cursor_col stays in view
;;; -----------------------------------------------------------------------
check_hscroll:
        push ax
        mov ax, [cursor_col]
        cmp ax, [view_col]
        jb .scroll_left
        ;; If cursor_col >= view_col + EDIT_COLS: scroll right
        push bx
        mov bx, [view_col]
        add bx, EDIT_COLS
        cmp ax, bx
        pop bx
        jb .done
        mov ax, [cursor_col]
        sub ax, EDIT_COLS - 1
        mov [view_col], ax
        jmp .done
        .scroll_left:
        mov [view_col], ax
        .done:
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; check_scroll: scroll view down if cursor moved below visible area
;;; -----------------------------------------------------------------------
check_scroll:
        push ax
        mov ax, [view_line]
        add ax, EDIT_ROWS - 1
        cmp ax, [cursor_line]
        jae .done
        mov ax, [cursor_line]
        sub ax, EDIT_ROWS - 1
        mov [view_line], ax
        .done:
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; check_scroll_up: scroll view up if cursor moved above visible area
;;; -----------------------------------------------------------------------
check_scroll_up:
        push ax
        mov ax, [cursor_line]
        cmp ax, [view_line]
        jae .done
        mov [view_line], ax
        .done:
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; do_kill: kill from cursor to end of line (Ctrl+K)
;;; If cursor is at a \n, kills the \n (joining lines).
;;; Killed text is stored in kill_buf / kill_len.
;;; -----------------------------------------------------------------------
do_kill:
        push ax
        push bx
        push di
        mov word [kill_len], 0
        xor di, di             ; DI = index into kill buffer
        ;; Kill chars through end of line (including the \n)
        .kill_chars:
        mov bx, [gap_end]
        cmp bx, BUF_SIZE
        jae .done              ; nothing after cursor
        mov al, [BUF_BASE + bx]
        inc word [gap_end]
        mov byte [dirty], 1
        cmp di, KILL_BUF_SIZE
        jae .next              ; kill buffer full: keep deleting, stop storing
        mov [KILL_BUF + di], al
        inc di
        .next:
        cmp al, 0Ah
        jne .kill_chars        ; stop after consuming the \n
        .done:
        mov [kill_len], di
        pop di
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; do_yank: insert kill_buf contents at cursor (Ctrl+Y)
;;; -----------------------------------------------------------------------
do_yank:
        push ax
        push cx
        push si
        mov cx, [kill_len]
        test cx, cx
        jz .done
        xor si, si             ; SI = index into kill buffer
        .yank_loop:
        mov al, [KILL_BUF + si]
        call buf_insert
        inc si
        loop .yank_loop
        .done:
        pop si
        pop cx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; emit_decimal: print AX as decimal (no leading zeros, min 1 digit)
;;; -----------------------------------------------------------------------
emit_decimal:
        push ax
        push bx
        push cx
        push dx
        xor cx, cx
        mov bx, 10
        .divide:
        xor dx, dx
        div bx
        push dx
        inc cx
        test ax, ax
        jnz .divide
        .emit:
        pop dx
        add dl, '0'
        mov al, dl
        mov ah, SYS_IO_PUTC
        int 30h
        loop .emit
        pop dx
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; get_input: read one key and handle it
;;; -----------------------------------------------------------------------
get_input:
        push ax
        push bx
        push cx

        mov ah, SYS_IO_GETC
        int 30h

        ;; If quit-confirmation is pending: Ctrl+Q confirms, anything else cancels
        cmp byte [confirm_quit], 0
        je .check_key
        cmp al, 11h
        je .quit_now
        mov byte [confirm_quit], 0
        jmp .done

        .check_key:
        test al, al
        jz .extended
        cmp al, 0E0h
        je .extended

        cmp al, 01h            ; Ctrl+A: beginning of line
        je .do_bol
        cmp al, 02h            ; Ctrl+B: back one character
        je .do_left
        cmp al, 05h            ; Ctrl+E: end of line
        je .do_eol
        cmp al, 06h            ; Ctrl+F: forward one character
        je .do_right
        cmp al, 08h            ; Backspace
        je .do_backspace
        cmp al, 0Ah            ; Enter (LF)
        je .do_enter
        cmp al, 0Bh            ; Ctrl+K: kill to end of line
        je .do_kill
        cmp al, 0Dh            ; Enter (CR)
        je .do_enter
        cmp al, 0Eh            ; Ctrl+N: next line
        je .do_down
        cmp al, 10h            ; Ctrl+P: previous line
        je .do_up
        cmp al, 11h            ; Ctrl+Q: quit
        je .do_quit
        cmp al, 13h            ; Ctrl+S: save
        je .do_save
        cmp al, 19h            ; Ctrl+Y: yank
        je .do_yank
        cmp al, 7Fh            ; DEL (serial backspace)
        je .do_backspace
        cmp al, 20h
        jb .done               ; non-printing control char
        cmp al, 7Eh
        ja .done               ; above tilde
        call buf_insert
        jmp .done

        .extended:
        cmp ah, 48h            ; Up arrow
        je .do_up
        cmp ah, 50h            ; Down arrow
        je .do_down
        cmp ah, 4Bh            ; Left arrow
        je .do_left
        cmp ah, 4Dh            ; Right arrow
        je .do_right
        cmp ah, 53h            ; Delete key
        je .do_delete
        jmp .done

        .do_bol:
        call move_bol
        jmp .done

        .do_backspace:
        call buf_delete_before
        jmp .done

        .do_delete:
        call buf_delete_after
        jmp .done

        .do_down:
        call move_down
        jmp .done

        .do_enter:
        mov al, 0Ah
        call buf_insert
        jmp .done

        .do_eol:
        call move_eol
        jmp .done

        .do_kill:
        call do_kill
        jmp .done

        .do_left:
        call move_left
        jmp .done

        .do_quit:
        cmp byte [dirty], 0
        je .quit_now
        mov byte [confirm_quit], 1
        jmp .done
        .quit_now:
        pop cx
        pop bx
        pop ax
        mov ah, SYS_SCR_CLEAR
        int 30h
        mov ah, SYS_EXIT
        int 30h

        .do_right:
        call move_right
        jmp .done

        .do_save:
        call save_file
        jmp .done

        .do_up:
        call move_up
        jmp .done

        .do_yank:
        call do_yank
        jmp .done

        .done:
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; -----------------------------------------------------------------------
;;; move_bol: move cursor to beginning of current line (Ctrl+A)
;;; -----------------------------------------------------------------------
move_bol:
        push ax
        push bx
        .loop:
        cmp word [gap_start], 0
        je .done
        mov bx, [gap_start]
        dec bx
        mov al, [BUF_BASE + bx]
        cmp al, 0Ah
        je .done               ; char before cursor is \n: already at line start
        mov bx, [gap_end]
        dec bx
        mov [BUF_BASE + bx], al
        dec word [gap_start]
        dec word [gap_end]
        jmp .loop
        .done:
        mov word [cursor_col], 0
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; move_down: move cursor to same column on next line
;;; -----------------------------------------------------------------------
move_down:
        push ax
        push bx
        push cx
        mov cx, [cursor_col]   ; target column
        ;; Advance past chars until we hit a newline (or end of buffer)
        .to_newline:
        mov bx, [gap_end]
        cmp bx, BUF_SIZE
        jae .done              ; at end of buffer
        mov al, [BUF_BASE + bx]
        mov bx, [gap_start]
        mov [BUF_BASE + bx], al
        inc word [gap_start]
        inc word [gap_end]
        cmp al, 0Ah
        je .found_newline
        jmp .to_newline
        .found_newline:
        inc word [cursor_line]
        mov word [cursor_col], 0
        call check_scroll
        ;; Advance min(cx, line_length) columns
        .forward:
        test cx, cx
        jz .done
        mov bx, [gap_end]
        cmp bx, BUF_SIZE
        jae .done
        mov al, [BUF_BASE + bx]
        cmp al, 0Ah
        je .done
        mov bx, [gap_start]
        mov [BUF_BASE + bx], al
        inc word [gap_start]
        inc word [gap_end]
        inc word [cursor_col]
        dec cx
        jmp .forward
        .done:
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; -----------------------------------------------------------------------
;;; move_eol: move cursor to end of current line (Ctrl+E)
;;; -----------------------------------------------------------------------
move_eol:
        push ax
        push bx
        .loop:
        mov bx, [gap_end]
        cmp bx, BUF_SIZE
        jae .done              ; at end of buffer
        mov al, [BUF_BASE + bx]
        cmp al, 0Ah
        je .done               ; at \n: cursor is at end of line
        mov bx, [gap_start]
        mov [BUF_BASE + bx], al
        inc word [gap_start]
        inc word [gap_end]
        inc word [cursor_col]
        jmp .loop
        .done:
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; move_left: move cursor one character left
;;; -----------------------------------------------------------------------
move_left:
        push ax
        push bx
        cmp word [gap_start], 0
        je .done
        mov bx, [gap_start]
        dec bx
        mov al, [BUF_BASE + bx]
        mov bx, [gap_end]
        dec bx
        mov [BUF_BASE + bx], al
        dec word [gap_start]
        dec word [gap_end]
        cmp al, 0Ah
        je .crossed_newline
        cmp word [cursor_col], 0
        je .done
        dec word [cursor_col]
        jmp .done
        .crossed_newline:
        cmp word [cursor_line], 0
        je .done
        dec word [cursor_line]
        call recompute_col
        call check_scroll_up
        .done:
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; move_right: move cursor one character right
;;; -----------------------------------------------------------------------
move_right:
        push ax
        push bx
        mov bx, [gap_end]
        cmp bx, BUF_SIZE
        jae .done
        mov al, [BUF_BASE + bx]
        mov bx, [gap_start]
        mov [BUF_BASE + bx], al
        inc word [gap_start]
        inc word [gap_end]
        cmp al, 0Ah
        je .crossed_newline
        inc word [cursor_col]
        jmp .done
        .crossed_newline:
        inc word [cursor_line]
        mov word [cursor_col], 0
        call check_scroll
        .done:
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; move_up: move cursor to same column on previous line
;;; -----------------------------------------------------------------------
move_up:
        push ax
        push bx
        push cx
        cmp word [cursor_line], 0
        je .done
        mov cx, [cursor_col]   ; target column
        ;; Move left until we cross the newline ending the previous line
        .back_past_newline:
        cmp word [gap_start], 0
        je .done
        mov bx, [gap_start]
        dec bx
        mov al, [BUF_BASE + bx]
        mov bx, [gap_end]
        dec bx
        mov [BUF_BASE + bx], al
        dec word [gap_start]
        dec word [gap_end]
        cmp al, 0Ah
        je .found_prev_newline
        jmp .back_past_newline
        .found_prev_newline:
        ;; Now move left to start of previous line
        .back_to_line_start:
        cmp word [gap_start], 0
        je .at_start
        mov bx, [gap_start]
        dec bx
        mov al, [BUF_BASE + bx]
        cmp al, 0Ah
        je .at_start           ; hit newline ending the line before, stop here
        mov bx, [gap_end]
        dec bx
        mov [BUF_BASE + bx], al
        dec word [gap_start]
        dec word [gap_end]
        jmp .back_to_line_start
        .at_start:
        dec word [cursor_line]
        mov word [cursor_col], 0
        call check_scroll_up
        ;; Advance min(cx, line_length) columns
        .forward:
        test cx, cx
        jz .done
        mov bx, [gap_end]
        cmp bx, BUF_SIZE
        jae .done
        mov al, [BUF_BASE + bx]
        cmp al, 0Ah
        je .done
        mov bx, [gap_start]
        mov [BUF_BASE + bx], al
        inc word [gap_start]
        inc word [gap_end]
        inc word [cursor_col]
        dec cx
        jmp .forward
        .done:
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; recompute_col: set cursor_col by counting chars back to previous newline
;;; Call after cursor_line has been decremented (e.g. after backspace over \n)
;;; -----------------------------------------------------------------------
recompute_col:
        push ax
        push bx
        push cx
        xor cx, cx
        mov bx, [gap_start]
        .scan:
        test bx, bx
        jz .done
        dec bx
        mov al, [BUF_BASE + bx]
        cmp al, 0Ah
        je .done
        inc cx
        jmp .scan
        .done:
        mov [cursor_col], cx
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; render: full-screen redraw
;;; Clears screen, prints EDIT_ROWS lines starting at view_line,
;;; prints status bar, then repositions cursor.
;;; -----------------------------------------------------------------------
render:
        push ax
        push bx
        push cx
        push dx
        push si

        mov ah, SYS_SCR_CLEAR
        int 30h

        ;; Walk to start of view_line: count newlines from offset 0
        xor bx, bx             ; BX = logical offset
        mov cx, [view_line]
        test cx, cx
        jz .render_from
        .skip_lines:
        call buf_char_at       ; AL = char at BX, CF if end
        jc .render_from
        inc bx
        cmp al, 0Ah
        jne .skip_lines
        loop .skip_lines

        .render_from:
        ;; Print up to EDIT_ROWS rows
        call check_hscroll
        mov dx, EDIT_ROWS      ; DX = rows remaining
        .row_loop:
        ;; Print one row: skip view_col chars, print up to EDIT_COLS, scan to \n
        mov cx, [view_col]     ; CX = chars to skip (horizontal scroll)
        mov si, EDIT_COLS      ; SI = visible cols remaining
        .char_loop:
        call buf_char_at
        jc .row_eof
        inc bx
        cmp al, 0Ah
        je .row_newline
        test cx, cx
        jnz .hscroll_skip
        test si, si
        jz .char_loop          ; past right edge, keep scanning to \n
        mov ah, SYS_IO_PUTC
        int 30h
        dec si
        jmp .char_loop
        .hscroll_skip:
        dec cx
        jmp .char_loop
        .row_newline:
        test si, si
        jz .row_no_nl          ; printed full row, cursor already wrapped
        mov al, 0Ah
        mov ah, SYS_IO_PUTC
        int 30h
        .row_no_nl:
        dec dx
        jnz .row_loop
        jmp .status_bar
        .row_eof:
        ;; If we printed content on this row, account for it
        cmp si, EDIT_COLS
        je .row_pad            ; no content on this row, just pad
        dec dx                 ; this row consumed a display row
        test si, si
        jz .row_pad            ; full row: cursor already wrapped, skip \n
        mov al, 0Ah            ; partial row: \n to finish it
        mov ah, SYS_IO_PUTC
        int 30h
        .row_pad:
        test dx, dx
        jz .status_bar
        mov al, 0Ah
        mov ah, SYS_IO_PUTC
        int 30h
        dec dx
        jnz .row_pad

        .status_bar:
        ;; Print status bar on row 24 (no trailing newline)
        cmp byte [confirm_quit], 0
        jne .status_confirm
        ;; Check for a one-shot status message
        cmp word [status_msg], 0
        je .status_normal
        mov si, [status_msg]
        mov ah, SYS_IO_PUTS
        int 30h
        mov word [status_msg], 0
        jmp .reposition
        .status_normal:
        ;; Normal status: "filename [modified]  line N"
        mov si, [filename]
        mov ah, SYS_IO_PUTS
        int 30h
        cmp byte [dirty], 0
        je .status_line_num
        mov si, MSG_MODIFIED
        mov ah, SYS_IO_PUTS
        int 30h
        .status_line_num:
        mov si, MSG_LINE
        mov ah, SYS_IO_PUTS
        int 30h
        mov ax, [cursor_line]
        inc ax
        call emit_decimal
        mov si, MSG_COL
        mov ah, SYS_IO_PUTS
        int 30h
        mov ax, [cursor_col]
        inc ax
        call emit_decimal
        jmp .reposition
        .status_confirm:
        mov si, MSG_UNSAVED
        mov ah, SYS_IO_PUTS
        int 30h

        ;; Reposition cursor: we're at end of status bar on row 24.
        ;; Emit \r to go to col 0 of row 24.
        .reposition:
        mov al, 0Dh
        mov ah, SYS_IO_PUTC
        int 30h
        ;; Compute cursor screen row = cursor_line - view_line
        mov ax, [cursor_line]
        sub ax, [view_line]    ; AX = cursor_screen_row (0-based)
        ;; Emit ESC[nA to move up (24 - cursor_screen_row) rows
        mov bx, 24
        sub bx, ax             ; BX = rows to move up
        test bx, bx
        jz .no_up
        mov al, 1Bh
        mov ah, SYS_IO_PUTC
        int 30h
        mov al, '['
        mov ah, SYS_IO_PUTC
        int 30h
        mov ax, bx
        call emit_decimal
        mov al, 'A'
        mov ah, SYS_IO_PUTC
        int 30h
        .no_up:
        ;; Emit ESC[nC to move to cursor screen col = cursor_col - view_col
        mov bx, [cursor_col]
        sub bx, [view_col]
        test bx, bx
        jz .render_done
        mov al, 1Bh
        mov ah, SYS_IO_PUTC
        int 30h
        mov al, '['
        mov ah, SYS_IO_PUTC
        int 30h
        mov ax, bx
        call emit_decimal
        mov al, 'C'
        mov ah, SYS_IO_PUTC
        int 30h

        .render_done:
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; save_file: write gap buffer content to disk
;;; Updates directory entry size. Cannot grow beyond original sector count.
;;; -----------------------------------------------------------------------
save_file:
        push ax
        push bx
        push cx
        push dx
        push di

        ;; Compute new content length
        call buf_length        ; AX = new size in bytes

        ;; Compute orig_sectors = ceil(orig_size / 512)
        mov cx, [orig_size]
        add cx, 511
        push ax
        xor dx, dx
        mov ax, cx
        mov bx, 512
        div bx                 ; AX = orig_sectors
        mov cx, ax
        pop ax

        ;; Compute new_sectors = ceil(new_size / 512)
        push cx
        push ax
        add ax, 511
        xor dx, dx
        mov bx, 512
        div bx                 ; AX = new_sectors
        mov dx, ax
        pop ax
        pop cx

        cmp dx, cx
        ja .too_big

        ;; Write data sectors
        mov al, [file_sector]
        xor bx, bx             ; BX = logical offset into content
        .write_loop:
        ;; Fill DISK_BUFFER with up to 512 bytes of content
        push ax
        push bx
        mov di, DISK_BUFFER
        mov cx, 512
        .fill:
        call buf_char_at       ; AL = char at BX, CF if end
        jc .fill_done
        mov [di], al
        inc di
        inc bx
        dec cx
        jnz .fill
        .fill_done:
        test cx, cx
        jz .do_write
        xor al, al
        rep stosb              ; zero-pad remainder
        .do_write:
        pop bx
        pop ax
        push ax
        push bx
        mov ah, SYS_FS_WRITE
        int 30h
        pop bx
        pop ax
        jc .write_err
        inc al
        add bx, 512
        ;; Check if all content written
        push ax
        call buf_length
        cmp bx, ax
        pop ax
        jb .write_loop

        ;; Re-read directory and update size in entry
        mov si, [filename]
        mov ah, SYS_FS_FIND
        int 30h
        jc .dir_err
        call buf_length
        mov [bx+14], ax        ; update size field in DISK_BUFFER (directory)
        mov al, DIR_SECTOR
        mov ah, SYS_FS_WRITE
        int 30h
        jc .write_err

        mov byte [dirty], 0
        mov word [status_msg], MSG_SAVED
        jmp .done

        .too_big:
        mov word [status_msg], MSG_TOO_BIG
        jmp .done

        .write_err:
        mov word [status_msg], MSG_WRITE_ERR
        jmp .done

        .dir_err:
        mov word [status_msg], MSG_WRITE_ERR

        .done:
        pop di
        pop dx
        pop cx
        pop bx
        pop ax
        ret

;;; -----------------------------------------------------------------------
;;; Variables (sorted)
;;; -----------------------------------------------------------------------
        confirm_quit  db 0
        cursor_col    dw 0
        cursor_line   dw 0
        dirty         db 0
        file_sector   db 0
        filename      dw 0
        gap_end       dw BUF_SIZE
        gap_start     dw 0
        kill_len      dw 0
        orig_size     dw 0
        status_msg    dw 0
        view_col      dw 0
        view_line     dw 0

;;; -----------------------------------------------------------------------
;;; Strings (sorted)
;;; -----------------------------------------------------------------------
        MSG_COL          db `  col \0`
        MSG_LINE         db `  line \0`
        MSG_LOAD_ERR     db `Load error\n\0`
        MSG_MODIFIED     db ` [modified]\0`
        MSG_NEW_FILE_ERR db `New files not supported: create with add_file.sh first\n\0`
        MSG_SAVED        db `Saved.\0`
        MSG_TOO_BIG      db `File too large to save (exceeds original size)\0`
        MSG_UNSAVED      db `Unsaved changes. Ctrl+Q again to quit.\0`
        MSG_USAGE        db `Usage: edit <filename>\n\0`
        MSG_WRITE_ERR    db `Write error\0`
