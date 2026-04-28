        [bits 32]
        org 0600h

%include "constants.asm"

        ;; Gap buffer in extended memory [EDIT_BUFFER_BASE ..
        ;; EDIT_BUFFER_BASE+EDIT_BUFFER_SIZE):
        ;;   [0 .. gap_start)        text before cursor
        ;;   [gap_start .. gap_end)  gap (free space)
        ;;   [gap_end .. EDIT_BUFFER_SIZE)  text after cursor
        ;; Constants from include/constants.asm (set by the pmode merge):
        ;;   EDIT_BUFFER_BASE      = 0x100000  (1 MB mark, past VGA / BIOS)
        ;;   EDIT_BUFFER_SIZE      = 0x100000  (1 MB gap buffer)
        ;;   EDIT_KILL_BUFFER      = 0x200000  (kill buffer at the 2 MB mark)
        ;;   EDIT_KILL_BUFFER_SIZE = 0xA00     (2560 bytes)

        ;; Screen layout: rows 0–23 for text, row 24 for status bar
        %assign EDIT_ROWS 24
        %assign EDIT_COLS 80

main:
        cld

        ;; Open /dev/vga up front so the render loop and quit path can
        ;; issue SYS_IO_IOCTL / VGA_IOCTL_MODE without reopening.
        mov esi, DEV_VGA
        mov al, O_WRONLY
        mov ah, SYS_IO_OPEN
        int 30h
        mov [vga_fd], eax

        ;; Require exactly one argument
        mov edi, ARGV
        call FUNCTION_PARSE_ARGV
        cmp ecx, 1
        jne .usage
        mov ebx, [ARGV]
        mov [filename], ebx

        ;; Try to open the file for reading
        mov esi, ebx
        mov al, O_RDONLY
        mov ah, SYS_IO_OPEN
        int 30h
        jc .new_file            ; file not found -- create on first save

        ;; Get file size via fstat.  Returns AL = mode, CX:DX = size
        ;; (low 16 of saved ECX / EDX).  The high halves of saved
        ;; ECX/EDX carry whatever pre-syscall held — zero them first
        ;; so we can compose the full 32-bit size cleanly below.
        mov ebx, eax            ; EBX = fd
        xor ecx, ecx
        xor edx, edx
        mov ah, SYS_IO_FSTAT
        int 30h
        ;; Compose 32-bit size in EDX: (cx << 16) | dx.
        movzx ecx, cx
        shl ecx, 16
        movzx edx, dx
        or edx, ecx
        cmp edx, EDIT_BUFFER_SIZE
        ja .too_big_close
        test al, FLAG_DIRECTORY
        jnz .is_dir_close

        ;; Load file content into gap buffer: text goes AFTER the gap so
        ;; gap_start=0 and cursor_line/col=0 are consistent (cursor at start).
        ;; gap_start = 0, gap_end = EDIT_BUFFER_SIZE - file_size
        mov dword [gap_start], 0
        mov eax, EDIT_BUFFER_SIZE
        sub eax, edx            ; EAX = EDIT_BUFFER_SIZE - file_size
        mov [gap_end], eax

        ;; Read entire file into EDIT_BUFFER_BASE + gap_end.
        mov edi, EDIT_BUFFER_BASE
        add edi, [gap_end]
        mov ecx, edx            ; ECX = file size (bytes to read)
        mov ah, SYS_IO_READ
        int 30h
        push eax                ; save bytes-read result
        ;; Close the file
        mov ah, SYS_IO_CLOSE
        int 30h
        pop eax
        cmp eax, -1
        je .load_err
        jmp .init_cursor

        .too_big_close:
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp .too_big

        .is_dir_close:
        mov ah, SYS_IO_CLOSE
        int 30h
        jmp .is_dir

        .new_file:
        ;; Defer file creation until first save
        mov dword [gap_start], 0
        mov dword [gap_end], EDIT_BUFFER_SIZE
        .init_cursor:
        ;; Set cursor to start of file
        mov dword [cursor_column], 0
        mov dword [cursor_line], 0
        mov dword [view_line], 0
        mov dword [view_column], 0

        .editor_loop:
        call render
        call get_input
        jmp .editor_loop

        .too_big:
        mov esi, MESSAGE_FILE_TOO_BIG
        mov ecx, MESSAGE_FILE_TOO_BIG_LENGTH
        jmp .print_exit

        .is_dir:
        mov esi, MESSAGE_IS_DIR
        mov ecx, MESSAGE_IS_DIR_LENGTH
        jmp .print_exit

        .load_err:
        mov esi, MESSAGE_LOAD_ERROR
        mov ecx, MESSAGE_LOAD_ERROR_LENGTH
        jmp .print_exit

        .usage:
        mov esi, MESSAGE_USAGE
        mov ecx, MESSAGE_USAGE_LENGTH

        .print_exit:
        jmp FUNCTION_DIE

;;; -----------------------------------------------------------------------
;;; buf_char_at: get logical char at offset EBX
;;; Returns AL = char, CF set if EBX >= logical length
;;; Preserves all registers except AL and flags
;;; -----------------------------------------------------------------------
buf_char_at:
        push ebx
        push esi
        ;; Compute logical length into ESI
        mov esi, [gap_end]
        sub esi, [gap_start]    ; ESI = gap size
        neg esi
        add esi, EDIT_BUFFER_SIZE  ; ESI = logical length
        cmp ebx, esi
        jae .past_end
        ;; Map logical offset EBX to raw index
        cmp ebx, [gap_start]
        jb .before_gap
        mov esi, [gap_end]
        sub esi, [gap_start]
        add ebx, esi            ; raw index = EBX + gap_size
        .before_gap:
        mov al, [EDIT_BUFFER_BASE + ebx]
        clc
        pop esi
        pop ebx
        ret
        .past_end:
        stc
        pop esi
        pop ebx
        ret

;;; -----------------------------------------------------------------------
;;; buf_delete_after: delete char after cursor (Delete key)
;;; -----------------------------------------------------------------------
buf_delete_after:
        push ebx
        mov ebx, [gap_end]
        cmp ebx, EDIT_BUFFER_SIZE
        jae .done               ; nothing after cursor
        inc dword [gap_end]
        mov byte [dirty], 1
        .done:
        pop ebx
        ret

;;; -----------------------------------------------------------------------
;;; buf_delete_before: delete char before cursor (Backspace)
;;; -----------------------------------------------------------------------
buf_delete_before:
        push eax
        push ebx
        cmp dword [gap_start], 0
        je .done
        mov ebx, [gap_start]
        dec ebx
        mov al, [EDIT_BUFFER_BASE + ebx]
        dec dword [gap_start]
        mov byte [dirty], 1
        cmp al, 0Ah
        je .was_newline
        cmp dword [cursor_column], 0
        je .done
        dec dword [cursor_column]
        jmp .done
        .was_newline:
        cmp dword [cursor_line], 0
        je .done
        dec dword [cursor_line]
        call recompute_column
        call check_scroll_up
        .done:
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; buf_insert: insert AL at cursor
;;; -----------------------------------------------------------------------
buf_insert:
        push eax
        push ebx
        mov ebx, [gap_start]
        cmp ebx, [gap_end]
        je .full                ; buffer full
        mov [EDIT_BUFFER_BASE + ebx], al
        inc dword [gap_start]
        mov byte [dirty], 1
        cmp al, 0Ah
        je .newline
        inc dword [cursor_column]
        jmp .done
        .newline:
        inc dword [cursor_line]
        mov dword [cursor_column], 0
        call check_scroll
        .done:
        pop ebx
        pop eax
        ret
        .full:
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; buf_length: logical text length into EAX
;;; -----------------------------------------------------------------------
buf_length:
        push ebx
        mov eax, EDIT_BUFFER_SIZE
        mov ebx, [gap_end]
        sub ebx, [gap_start]
        sub eax, ebx
        pop ebx
        ret

;;; -----------------------------------------------------------------------
;;; check_hscroll: adjust view_column so cursor_column stays in view
;;; -----------------------------------------------------------------------
check_hscroll:
        push eax
        mov eax, [cursor_column]
        cmp eax, [view_column]
        jb .scroll_left
        ;; If cursor_column >= view_column + EDIT_COLS: scroll right
        push ebx
        mov ebx, [view_column]
        add ebx, EDIT_COLS
        cmp eax, ebx
        pop ebx
        jb .done
        mov eax, [cursor_column]
        sub eax, EDIT_COLS - 1
        mov [view_column], eax
        jmp .done
        .scroll_left:
        mov [view_column], eax
        .done:
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; check_scroll: scroll view down if cursor moved below visible area
;;; -----------------------------------------------------------------------
check_scroll:
        push eax
        mov eax, [view_line]
        add eax, EDIT_ROWS - 1
        cmp eax, [cursor_line]
        jae .done
        mov eax, [cursor_line]
        sub eax, EDIT_ROWS - 1
        mov [view_line], eax
        .done:
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; check_scroll_up: scroll view up if cursor moved above visible area
;;; -----------------------------------------------------------------------
check_scroll_up:
        push eax
        mov eax, [cursor_line]
        cmp eax, [view_line]
        jae .done
        mov [view_line], eax
        .done:
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; do_kill: kill from cursor to end of line (Ctrl+K)
;;; If cursor is at a \n, kills the \n (joining lines).
;;; Killed text is stored in EDIT_KILL_BUFFER / kill_length.
;;; -----------------------------------------------------------------------
do_kill:
        push eax
        push ebx
        push edi
        mov dword [kill_length], 0
        xor edi, edi            ; EDI = index into kill buffer
        ;; Kill chars through end of line (including the \n)
        .kill_chars:
        mov ebx, [gap_end]
        cmp ebx, EDIT_BUFFER_SIZE
        jae .done               ; nothing after cursor
        mov al, [EDIT_BUFFER_BASE + ebx]
        inc dword [gap_end]
        mov byte [dirty], 1
        cmp edi, EDIT_KILL_BUFFER_SIZE
        jae .next               ; kill buffer full: keep deleting, stop storing
        mov [EDIT_KILL_BUFFER + edi], al
        inc edi
        .next:
        cmp al, 0Ah
        jne .kill_chars         ; stop after consuming the \n
        .done:
        mov [kill_length], edi
        pop edi
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; do_yank: insert kill buffer contents at cursor (Ctrl+Y)
;;; -----------------------------------------------------------------------
do_yank:
        push eax
        push ecx
        push esi
        mov ecx, [kill_length]
        test ecx, ecx
        jz .done
        xor esi, esi            ; ESI = index into kill buffer
        .yank_loop:
        mov al, [EDIT_KILL_BUFFER + esi]
        call buf_insert
        inc esi
        loop .yank_loop
        .done:
        pop esi
        pop ecx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; emit_decimal: print EAX as decimal (no leading zeros, min 1 digit)
;;; -----------------------------------------------------------------------
emit_decimal:
        push eax
        push ebx
        push ecx
        push edx
        xor ecx, ecx
        mov ebx, 10
        .divide:
        xor edx, edx
        div ebx
        push edx
        inc ecx
        test eax, eax
        jnz .divide
        .emit:
        pop edx
        add dl, '0'
        mov al, dl
        call FUNCTION_PRINT_CHARACTER
        loop .emit
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; get_input: read one key and handle it
;;; -----------------------------------------------------------------------
get_input:
        push eax
        push ebx
        push ecx

        call FUNCTION_GET_CHARACTER

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

        cmp al, 01h             ; Ctrl+A: beginning of line
        je .do_bol
        cmp al, 02h             ; Ctrl+B: back one character
        je .do_left
        cmp al, 05h             ; Ctrl+E: end of line
        je .do_eol
        cmp al, 06h             ; Ctrl+F: forward one character
        je .do_right
        cmp al, 08h             ; Backspace
        je .do_backspace
        cmp al, 0Ah             ; Enter (LF)
        je .do_enter
        cmp al, 0Bh             ; Ctrl+K: kill to end of line
        je .do_kill
        cmp al, 0Dh             ; Enter (CR)
        je .do_enter
        cmp al, 0Eh             ; Ctrl+N: next line
        je .do_down
        cmp al, 10h             ; Ctrl+P: previous line
        je .do_up
        cmp al, 11h             ; Ctrl+Q: quit
        je .do_quit
        cmp al, 13h             ; Ctrl+S: save
        je .do_save
        cmp al, 19h             ; Ctrl+Y: yank
        je .do_yank
        cmp al, 7Fh             ; DEL (serial backspace)
        je .do_backspace
        cmp al, 20h
        jb .done                ; non-printing control char
        cmp al, 7Eh
        ja .done                ; above tilde
        call buf_insert
        jmp .done

        .extended:
        cmp ah, 48h             ; Up arrow
        je .do_up
        cmp ah, 50h             ; Down arrow
        je .do_down
        cmp ah, 4Bh             ; Left arrow
        je .do_left
        cmp ah, 4Dh             ; Right arrow
        je .do_right
        cmp ah, 53h             ; Delete key
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
        pop ecx
        pop ebx
        pop eax
        mov ebx, [vga_fd]
        mov dl, VIDEO_MODE_TEXT_80x25
        mov al, VGA_IOCTL_MODE
        mov ah, SYS_IO_IOCTL
        int 30h
        jmp FUNCTION_EXIT

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
        pop ecx
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; move_bol: move cursor to beginning of current line (Ctrl+A)
;;; -----------------------------------------------------------------------
move_bol:
        push eax
        push ebx
        .loop:
        cmp dword [gap_start], 0
        je .done
        mov ebx, [gap_start]
        dec ebx
        mov al, [EDIT_BUFFER_BASE + ebx]
        cmp al, 0Ah
        je .done                ; char before cursor is \n: already at line start
        mov ebx, [gap_end]
        dec ebx
        mov [EDIT_BUFFER_BASE + ebx], al
        dec dword [gap_start]
        dec dword [gap_end]
        jmp .loop
        .done:
        mov dword [cursor_column], 0
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; move_down: move cursor to same column on next line
;;; -----------------------------------------------------------------------
move_down:
        push eax
        push ebx
        push ecx
        mov ecx, [cursor_column]   ; target column
        ;; Advance past chars until we hit a newline (or end of buffer)
        .to_newline:
        mov ebx, [gap_end]
        cmp ebx, EDIT_BUFFER_SIZE
        jae .done               ; at end of buffer
        mov al, [EDIT_BUFFER_BASE + ebx]
        mov ebx, [gap_start]
        mov [EDIT_BUFFER_BASE + ebx], al
        inc dword [gap_start]
        inc dword [gap_end]
        cmp al, 0Ah
        je .found_newline
        jmp .to_newline
        .found_newline:
        inc dword [cursor_line]
        mov dword [cursor_column], 0
        call check_scroll
        ;; Advance min(ecx, line_length) columns
        .forward:
        test ecx, ecx
        jz .done
        mov ebx, [gap_end]
        cmp ebx, EDIT_BUFFER_SIZE
        jae .done
        mov al, [EDIT_BUFFER_BASE + ebx]
        cmp al, 0Ah
        je .done
        mov ebx, [gap_start]
        mov [EDIT_BUFFER_BASE + ebx], al
        inc dword [gap_start]
        inc dword [gap_end]
        inc dword [cursor_column]
        dec ecx
        jmp .forward
        .done:
        pop ecx
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; move_eol: move cursor to end of current line (Ctrl+E)
;;; -----------------------------------------------------------------------
move_eol:
        push eax
        push ebx
        .loop:
        mov ebx, [gap_end]
        cmp ebx, EDIT_BUFFER_SIZE
        jae .done               ; at end of buffer
        mov al, [EDIT_BUFFER_BASE + ebx]
        cmp al, 0Ah
        je .done                ; at \n: cursor is at end of line
        mov ebx, [gap_start]
        mov [EDIT_BUFFER_BASE + ebx], al
        inc dword [gap_start]
        inc dword [gap_end]
        inc dword [cursor_column]
        jmp .loop
        .done:
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; move_left: move cursor one character left
;;; -----------------------------------------------------------------------
move_left:
        push eax
        push ebx
        cmp dword [gap_start], 0
        je .done
        mov ebx, [gap_start]
        dec ebx
        mov al, [EDIT_BUFFER_BASE + ebx]
        mov ebx, [gap_end]
        dec ebx
        mov [EDIT_BUFFER_BASE + ebx], al
        dec dword [gap_start]
        dec dword [gap_end]
        cmp al, 0Ah
        je .crossed_newline
        cmp dword [cursor_column], 0
        je .done
        dec dword [cursor_column]
        jmp .done
        .crossed_newline:
        cmp dword [cursor_line], 0
        je .done
        dec dword [cursor_line]
        call recompute_column
        call check_scroll_up
        .done:
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; move_right: move cursor one character right
;;; -----------------------------------------------------------------------
move_right:
        push eax
        push ebx
        mov ebx, [gap_end]
        cmp ebx, EDIT_BUFFER_SIZE
        jae .done
        mov al, [EDIT_BUFFER_BASE + ebx]
        mov ebx, [gap_start]
        mov [EDIT_BUFFER_BASE + ebx], al
        inc dword [gap_start]
        inc dword [gap_end]
        cmp al, 0Ah
        je .crossed_newline
        inc dword [cursor_column]
        jmp .done
        .crossed_newline:
        inc dword [cursor_line]
        mov dword [cursor_column], 0
        call check_scroll
        .done:
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; move_up: move cursor to same column on previous line
;;; -----------------------------------------------------------------------
move_up:
        push eax
        push ebx
        push ecx
        cmp dword [cursor_line], 0
        je .done
        mov ecx, [cursor_column]   ; target column
        ;; Move left until we cross the newline ending the previous line
        .back_past_newline:
        cmp dword [gap_start], 0
        je .done
        mov ebx, [gap_start]
        dec ebx
        mov al, [EDIT_BUFFER_BASE + ebx]
        mov ebx, [gap_end]
        dec ebx
        mov [EDIT_BUFFER_BASE + ebx], al
        dec dword [gap_start]
        dec dword [gap_end]
        cmp al, 0Ah
        je .found_prev_newline
        jmp .back_past_newline
        .found_prev_newline:
        ;; Now move left to start of previous line
        .back_to_line_start:
        cmp dword [gap_start], 0
        je .at_start
        mov ebx, [gap_start]
        dec ebx
        mov al, [EDIT_BUFFER_BASE + ebx]
        cmp al, 0Ah
        je .at_start            ; hit newline ending the line before, stop here
        mov ebx, [gap_end]
        dec ebx
        mov [EDIT_BUFFER_BASE + ebx], al
        dec dword [gap_start]
        dec dword [gap_end]
        jmp .back_to_line_start
        .at_start:
        dec dword [cursor_line]
        mov dword [cursor_column], 0
        call check_scroll_up
        ;; Advance min(ecx, line_length) columns
        .forward:
        test ecx, ecx
        jz .done
        mov ebx, [gap_end]
        cmp ebx, EDIT_BUFFER_SIZE
        jae .done
        mov al, [EDIT_BUFFER_BASE + ebx]
        cmp al, 0Ah
        je .done
        mov ebx, [gap_start]
        mov [EDIT_BUFFER_BASE + ebx], al
        inc dword [gap_start]
        inc dword [gap_end]
        inc dword [cursor_column]
        dec ecx
        jmp .forward
        .done:
        pop ecx
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; recompute_column: set cursor_column by counting chars back to previous newline
;;; Call after cursor_line has been decremented (e.g. after backspace over \n)
;;; -----------------------------------------------------------------------
recompute_column:
        push eax
        push ebx
        push ecx
        xor ecx, ecx
        mov ebx, [gap_start]
        .scan:
        test ebx, ebx
        jz .done
        dec ebx
        mov al, [EDIT_BUFFER_BASE + ebx]
        cmp al, 0Ah
        je .done
        inc ecx
        jmp .scan
        .done:
        mov [cursor_column], ecx
        pop ecx
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; render: full-screen redraw
;;; Clears screen, prints EDIT_ROWS lines starting at view_line,
;;; prints status bar, then repositions cursor.
;;; -----------------------------------------------------------------------
render:
        push eax
        push ebx
        push ecx
        push edx
        push esi

        mov ebx, [vga_fd]
        mov dl, VIDEO_MODE_TEXT_80x25
        mov al, VGA_IOCTL_MODE
        mov ah, SYS_IO_IOCTL
        int 30h

        ;; Walk to start of view_line: count newlines from offset 0
        xor ebx, ebx            ; EBX = logical offset
        mov ecx, [view_line]
        test ecx, ecx
        jz .render_from
        .skip_lines:
        call buf_char_at        ; AL = char at EBX, CF if end
        jc .render_from
        inc ebx
        cmp al, 0Ah
        jne .skip_lines
        loop .skip_lines

        .render_from:
        ;; Print up to EDIT_ROWS rows
        call check_hscroll
        mov edx, EDIT_ROWS      ; EDX = rows remaining
        .row_loop:
        ;; Print one row: skip view_column chars, print up to EDIT_COLS, scan to \n
        mov ecx, [view_column]  ; ECX = chars to skip (horizontal scroll)
        mov esi, EDIT_COLS      ; ESI = visible cols remaining
        .char_loop:
        call buf_char_at
        jc .row_eof
        inc ebx
        cmp al, 0Ah
        je .row_newline
        test ecx, ecx
        jnz .hscroll_skip
        test esi, esi
        jz .char_loop           ; past right edge, keep scanning to \n
        call FUNCTION_PRINT_CHARACTER
        dec esi
        jmp .char_loop
        .hscroll_skip:
        dec ecx
        jmp .char_loop
        .row_newline:
        test esi, esi
        jz .row_no_nl           ; printed full row, cursor already wrapped
        mov al, 0Ah
        call FUNCTION_PRINT_CHARACTER
        .row_no_nl:
        dec edx
        jnz .row_loop
        jmp .status_bar
        .row_eof:
        ;; If we printed content on this row, account for it
        cmp esi, EDIT_COLS
        je .row_pad             ; no content on this row, just pad
        dec edx                 ; this row consumed a display row
        test esi, esi
        jz .row_pad             ; full row: cursor already wrapped, skip \n
        mov al, 0Ah             ; partial row: \n to finish it
        call FUNCTION_PRINT_CHARACTER
        .row_pad:
        test edx, edx
        jz .status_bar
        mov al, 0Ah
        call FUNCTION_PRINT_CHARACTER
        dec edx
        jnz .row_pad

        .status_bar:
        ;; Print status bar on row 24 (no trailing newline)
        cmp byte [confirm_quit], 0
        jne .status_confirm
        ;; Check for a one-shot status message
        cmp dword [status_message], 0
        je .status_normal
        mov edi, [status_message]
        call FUNCTION_PRINT_STRING
        mov dword [status_message], 0
        jmp .reposition
        .status_normal:
        ;; Normal status: "filename [modified]  line N  col M"
        mov edi, [filename]
        call FUNCTION_PRINT_STRING
        cmp byte [dirty], 0
        je .status_line_num
        mov esi, MESSAGE_MODIFIED
        mov ecx, MESSAGE_MODIFIED_LENGTH
        call FUNCTION_WRITE_STDOUT
        .status_line_num:
        mov esi, MESSAGE_LINE
        mov ecx, MESSAGE_LINE_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov eax, [cursor_line]
        inc eax
        call emit_decimal
        mov esi, MESSAGE_COLUMN
        mov ecx, MESSAGE_COLUMN_LENGTH
        call FUNCTION_WRITE_STDOUT
        mov eax, [cursor_column]
        inc eax
        call emit_decimal
        jmp .reposition
        .status_confirm:
        mov esi, MESSAGE_UNSAVED
        mov ecx, MESSAGE_UNSAVED_LENGTH
        call FUNCTION_WRITE_STDOUT

        ;; Reposition cursor: we're at end of status bar on row 24.
        ;; Emit \r to go to col 0 of row 24.
        .reposition:
        mov al, 0Dh
        call FUNCTION_PRINT_CHARACTER
        ;; Compute cursor screen row = cursor_line - view_line
        mov eax, [cursor_line]
        sub eax, [view_line]    ; EAX = cursor_screen_row (0-based)
        ;; Emit ESC[nA to move up (24 - cursor_screen_row) rows
        mov ebx, 24
        sub ebx, eax            ; EBX = rows to move up
        test ebx, ebx
        jz .no_up
        mov al, 1Bh
        call FUNCTION_PRINT_CHARACTER
        mov al, '['
        call FUNCTION_PRINT_CHARACTER
        mov eax, ebx
        call emit_decimal
        mov al, 'A'
        call FUNCTION_PRINT_CHARACTER
        .no_up:
        ;; Emit ESC[nC to move to cursor screen col = cursor_column - view_column
        mov ebx, [cursor_column]
        sub ebx, [view_column]
        test ebx, ebx
        jz .render_done
        mov al, 1Bh
        call FUNCTION_PRINT_CHARACTER
        mov al, '['
        call FUNCTION_PRINT_CHARACTER
        mov eax, ebx
        call emit_decimal
        mov al, 'C'
        call FUNCTION_PRINT_CHARACTER

        .render_done:
        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; save_file: write gap buffer content to disk
;;; Updates directory entry size. Cannot grow beyond original sector count.
;;; -----------------------------------------------------------------------
save_file:
        push eax
        push ebx
        push ecx
        push edx
        push edi

        ;; Open file for writing (create if new, truncate if existing)
        mov esi, [filename]
        mov al, O_WRONLY + O_CREAT + O_TRUNC
        xor dl, dl              ; mode = 0 (no exec flag)
        mov ah, SYS_IO_OPEN
        int 30h
        jc .create_err
        mov [save_fd], eax

        ;; Write content in 512-byte chunks via buf_char_at
        xor ebx, ebx            ; EBX = logical offset into content
        .write_loop:
        push ebx
        mov edi, SECTOR_BUFFER
        mov ecx, 512
        .fill:
        call buf_char_at        ; AL = char at EBX, CF if end
        jc .fill_done
        mov [edi], al
        inc edi
        inc ebx
        dec ecx
        jnz .fill
        .fill_done:
        ;; Compute chunk size: 512 - remaining
        mov eax, 512
        sub eax, ecx            ; EAX = bytes filled
        pop ebx
        test eax, eax
        jz .write_done          ; nothing left to write

        ;; Write the chunk
        push ebx
        mov ecx, eax            ; ECX = bytes to write
        mov ebx, [save_fd]
        mov esi, SECTOR_BUFFER
        mov ah, SYS_IO_WRITE
        int 30h
        pop ebx
        cmp eax, -1
        je .write_err

        add ebx, 512
        ;; Check if all content written
        push ebx
        call buf_length
        mov ecx, eax
        pop ebx
        cmp ebx, ecx
        jb .write_loop

        .write_done:
        ;; Close — kernel writes back the directory size automatically
        mov ebx, [save_fd]
        mov ah, SYS_IO_CLOSE
        int 30h

        mov byte [dirty], 0
        mov dword [status_message], MESSAGE_SAVED
        jmp .done

        .create_err:
        mov dword [status_message], MESSAGE_CREATE_ERROR
        jmp .done

        .write_err:
        mov ebx, [save_fd]
        mov ah, SYS_IO_CLOSE
        int 30h
        mov dword [status_message], MESSAGE_WRITE_ERROR

        .done:
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

;;; -----------------------------------------------------------------------
;;; Variables (sorted)
;;; -----------------------------------------------------------------------
        confirm_quit  db 0
        cursor_column dd 0
        cursor_line   dd 0
        dirty         db 0
        filename      dd 0
        gap_end       dd EDIT_BUFFER_SIZE
        gap_start     dd 0
        kill_length   dd 0
        save_fd       dd 0
        status_message dd 0
        vga_fd        dd 0
        view_column   dd 0
        view_line     dd 0

;;; -----------------------------------------------------------------------
;;; Strings (sorted)
;;; -----------------------------------------------------------------------
        DEV_VGA                 db `/dev/vga\0`
        MESSAGE_COLUMN          db `  col `
        MESSAGE_COLUMN_LENGTH   equ $ - MESSAGE_COLUMN
        MESSAGE_CREATE_ERROR   db `Cannot create file (directory full?)\0`
        MESSAGE_FILE_TOO_BIG db `File too large for edit buffer\n`
        MESSAGE_FILE_TOO_BIG_LENGTH equ $ - MESSAGE_FILE_TOO_BIG
        MESSAGE_IS_DIR       db `Is a directory\n`
        MESSAGE_IS_DIR_LENGTH equ $ - MESSAGE_IS_DIR
        MESSAGE_LINE         db `  line `
        MESSAGE_LINE_LENGTH  equ $ - MESSAGE_LINE
        MESSAGE_LOAD_ERROR     db `Load error\n`
        MESSAGE_LOAD_ERROR_LENGTH equ $ - MESSAGE_LOAD_ERROR
        MESSAGE_MODIFIED     db ` [modified]`
        MESSAGE_MODIFIED_LENGTH equ $ - MESSAGE_MODIFIED
        MESSAGE_SAVED        db `Saved.\0`
        MESSAGE_UNSAVED      db `Unsaved changes. Ctrl+Q again to quit.`
        MESSAGE_UNSAVED_LENGTH equ $ - MESSAGE_UNSAVED
        MESSAGE_USAGE        db `Usage: edit <filename>\n`
        MESSAGE_USAGE_LENGTH equ $ - MESSAGE_USAGE
        MESSAGE_WRITE_ERROR    db `Write error\0`
