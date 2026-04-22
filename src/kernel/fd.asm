;;; fd.asm -- File descriptor table management
;;;
;;; fd_alloc:         Find the first free FD slot (AX = fd number, CF if full)
;;; fd_close:         SYS_IO_CLOSE -- BX=fd; flushes writable files
;;; fd_fstat:         SYS_IO_FSTAT -- BX=fd; returns AL=mode, CX:DX=size
;;; fd_init:          Zero the FD table, pre-open fds 0/1/2 as console
;;; fd_lookup:        Validate fd in BX, return SI = entry pointer (CF if invalid)
;;; fd_open:          SYS_IO_OPEN  -- SI=filename, AL=flags, DL=mode; returns AX=fd
;;; fd_pos_to_sector: Convert fd_pos to sector + offset (internal helper)
;;; fd_read:          SYS_IO_READ  -- BX=fd, DI=buffer, CX=count; returns AX=bytes
;;; fd_write:         SYS_IO_WRITE -- BX=fd, SI=buffer, CX=count; returns AX=bytes

fd_alloc:
        ;; Find first free FD slot
        ;; Returns: AX = fd number, SI = entry pointer; CF set if table full
        push bx
        push cx
        mov si, fd_table
        xor ax, ax
        mov cx, FD_MAX
        .scan:
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_FREE
        je .found
        add si, FD_ENTRY_SIZE
        inc ax
        dec cx
        jnz .scan
        pop cx
        pop bx
        stc
        ret
        .found:
        pop cx
        pop bx
        clc
        ret

;;; -----------------------------------------------------------------------
;;; fd_close: Close a file descriptor
;;; Input:  BX = fd number
;;; Output: CF set on error
;;;
;;; For writable file FDs, updates the directory entry's size field
;;; from fd_pos (the number of bytes written) before freeing the slot.
;;; -----------------------------------------------------------------------
fd_close:
        call fd_lookup
        jc .close_err
        ;; SI = entry pointer
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_FILE
        jne .close_free
        test byte [si+FD_OFFSET_FLAGS], O_WRONLY
        jz .close_free
        ;; Writable file — update directory entry size from fd_pos
        push ax
        push bx
        push cx
        push dx
        ;; Read the directory sector containing this file's entry
        mov ax, [si+FD_OFFSET_DIRECTORY_SECTOR]
        mov [directory_loaded_sector], ax
        call read_sector
        jc .close_write_err
        ;; Point BX at the entry within SECTOR_BUFFER
        mov bx, SECTOR_BUFFER
        add bx, [si+FD_OFFSET_DIRECTORY_OFFSET]
        ;; Update size from fd_pos (32-bit)
        mov ax, [si+FD_OFFSET_POSITION]
        mov [bx+DIRECTORY_OFFSET_SIZE], ax
        mov ax, [si+FD_OFFSET_POSITION+2]
        mov [bx+DIRECTORY_OFFSET_SIZE+2], ax
        ;; Write back the directory sector
        call directory_write_back
        jc .close_write_err
        pop dx
        pop cx
        pop bx
        pop ax
        jmp .close_free
        .close_write_err:
        pop dx
        pop cx
        pop bx
        pop ax
        ;; Fall through to free the slot, but propagate error
        .close_free:
        ;; Zero the entry to free it
        push ax
        push cx
        push di
        mov di, si
        xor ax, ax
        mov cx, FD_ENTRY_SIZE / 2
        cld
        rep stosw
        pop di
        pop cx
        pop ax
        clc
        ret
        .close_err:
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_fstat: Get file status from a file descriptor
;;; Input:  BX = fd number
;;; Output: AL = mode (file permission flags), CX:DX = size (32-bit)
;;;         CF set on error
;;; -----------------------------------------------------------------------
fd_fstat:
        call fd_lookup
        jc .fstat_err
        ;; SI = entry pointer
        mov al, [si+FD_OFFSET_MODE]
        mov dx, [si+FD_OFFSET_SIZE]
        mov cx, [si+FD_OFFSET_SIZE+2]
        clc
        ret
        .fstat_err:
        stc
        ret

fd_init:
        ;; Zero the entire FD table
        push ax
        push cx
        push di
        mov di, fd_table
        xor ax, ax
        mov cx, FD_MAX * FD_ENTRY_SIZE / 2
        cld
        rep stosw
        ;; Pre-open fd 0 (stdin), fd 1 (stdout), fd 2 (stderr) as console
        mov si, fd_table
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFFSET_FLAGS], O_RDONLY
        add si, FD_ENTRY_SIZE
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFFSET_FLAGS], O_WRONLY
        add si, FD_ENTRY_SIZE
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        mov byte [si+FD_OFFSET_FLAGS], O_WRONLY
        pop di
        pop cx
        pop ax
        ret

fd_lookup:
        ;; Validate fd in BX, return SI = entry pointer
        ;; CF set if invalid (out of range or slot is free)
        cmp bx, FD_MAX
        jae .invalid
        push ax
        mov ax, bx
        shl ax, 5              ; ax = bx * FD_ENTRY_SIZE (32)
        mov si, fd_table
        add si, ax
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_FREE
        je .invalid_pop
        pop ax
        clc
        ret
        .invalid_pop:
        pop ax
        .invalid:
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_open: Open a file and return a file descriptor
;;; Input:  SI = filename, AL = flags (O_RDONLY, O_WRONLY, O_CREAT, O_TRUNC)
;;; Output: AX = fd number (CF clear), or -1 on error (CF set)
;;; -----------------------------------------------------------------------
fd_open:
        push cx
        push dx
        push di
        ;; Save flags and filename before subsequent calls clobber them
        mov [fd_open_flags], al
        mov [fd_open_name], si
        ;; Check for "." (root directory)
        cmp byte [si], '.'
        jne .open_find
        cmp byte [si+1], 0
        jne .open_find
        jmp .open_root_dir
        .open_find:
        ;; Look up the file
        call find_file
        jc .open_not_found
        ;; Found — open as DIR or FILE depending on entry flags
        jmp .open_populate

        .open_not_found:
        ;; If O_CREAT is set, create the file
        test byte [fd_open_flags], O_CREAT
        jz .open_err
        ;; Create: scan for free entry + next data sector
        mov si, [fd_open_name]
        call scan_directory_entries   ; BX = free root entry index, DX = next data sector
        mov [fd_open_sector], dx   ; save start sector for new file
        ;; Check for '/' in filename to handle subdirectory paths
        mov di, [fd_open_name]
        .open_find_slash:
        mov al, [di]
        test al, al
        jz .open_create_root
        cmp al, '/'
        je .open_create_subdir
        inc di
        jmp .open_find_slash

        .open_create_root:
        ;; Create in root directory
        cmp bx, 0FFFFh
        je .open_err            ; directory full
        call directory_load_entry     ; BX = entry ptr in SECTOR_BUFFER
        mov si, [fd_open_name]
        jmp .open_write_entry

        .open_create_subdir:
        ;; DI points to '/'. Null-terminate dirname, find subdir, create entry.
        mov byte [di], 0
        push di
        mov si, [fd_open_name]
        call find_file          ; find the subdirectory
        pop di
        mov byte [di], '/'
        jc .open_err
        test byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jz .open_err
        ;; Scan subdirectory for free entry
        mov ax, [bx+DIRECTORY_OFFSET_SECTOR]
        call subdir_find_free
        jc .open_err
        ;; BX = free entry ptr in SECTOR_BUFFER, directory_loaded_sector set
        inc di                  ; skip past '/'
        mov si, di              ; SI = basename
        jmp .open_write_entry

        .open_write_entry:
        ;; BX = entry ptr in SECTOR_BUFFER, SI = filename to write
        push bx
        call write_directory_name
        pop bx
        mov byte [bx+DIRECTORY_OFFSET_FLAGS], 0
        mov ax, [fd_open_sector]
        mov [bx+DIRECTORY_OFFSET_SECTOR], ax
        mov word [bx+DIRECTORY_OFFSET_SIZE], 0
        mov word [bx+DIRECTORY_OFFSET_SIZE+2], 0
        call directory_write_back
        jc .open_err
        ;; Now BX points to the new entry in SECTOR_BUFFER — fall through
        ;; to populate the FD

        .open_populate:
        ;; BX = dir entry in SECTOR_BUFFER
        call fd_alloc
        jc .open_err            ; table full
        ;; SI = FD entry pointer from fd_alloc, AX = fd number
        mov [fd_open_fd], ax
        ;; Set FD type: DIR if directory entry, FILE otherwise
        test byte [bx+DIRECTORY_OFFSET_FLAGS], FLAG_DIRECTORY
        jnz .open_set_dir
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_FILE
        jmp .open_set_flags
        .open_set_dir:
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_DIRECTORY
        .open_set_flags:
        mov cl, [fd_open_flags]
        mov [si+FD_OFFSET_FLAGS], cl
        ;; mode (file permission flags from directory entry)
        mov cl, [bx+DIRECTORY_OFFSET_FLAGS]
        mov [si+FD_OFFSET_MODE], cl
        ;; start_sec
        mov cx, [bx+DIRECTORY_OFFSET_SECTOR]
        mov [si+FD_OFFSET_START], cx
        ;; size (32-bit)
        mov cx, [bx+DIRECTORY_OFFSET_SIZE]
        mov [si+FD_OFFSET_SIZE], cx
        mov cx, [bx+DIRECTORY_OFFSET_SIZE+2]
        mov [si+FD_OFFSET_SIZE+2], cx
        ;; pos = 0
        mov word [si+FD_OFFSET_POSITION], 0
        mov word [si+FD_OFFSET_POSITION+2], 0
        ;; dir_sec and dir_off (for writeback on close)
        mov cx, [directory_loaded_sector]
        mov [si+FD_OFFSET_DIRECTORY_SECTOR], cx
        mov cx, bx
        sub cx, SECTOR_BUFFER
        mov [si+FD_OFFSET_DIRECTORY_OFFSET], cx
        ;; If O_TRUNC, reset size to 0
        test byte [fd_open_flags], O_TRUNC
        jz .open_done
        mov word [si+FD_OFFSET_SIZE], 0
        mov word [si+FD_OFFSET_SIZE+2], 0
        .open_done:
        mov ax, [fd_open_fd]
        pop di
        pop dx
        pop cx
        clc
        ret

        .open_root_dir:
        ;; Synthesize a DIR fd for the root directory
        call fd_alloc
        jc .open_err
        mov [fd_open_fd], ax
        mov byte [si+FD_OFFSET_TYPE], FD_TYPE_DIRECTORY
        mov cl, [fd_open_flags]
        mov [si+FD_OFFSET_FLAGS], cl
        mov byte [si+FD_OFFSET_MODE], FLAG_DIRECTORY
        mov word [si+FD_OFFSET_START], DIRECTORY_SECTOR
        mov word [si+FD_OFFSET_SIZE], DIRECTORY_SECTORS * 512
        mov word [si+FD_OFFSET_SIZE+2], 0
        mov word [si+FD_OFFSET_POSITION], 0
        mov word [si+FD_OFFSET_POSITION+2], 0
        mov word [si+FD_OFFSET_DIRECTORY_SECTOR], 0
        mov word [si+FD_OFFSET_DIRECTORY_OFFSET], 0
        jmp .open_done

        .open_err:
        pop di
        pop dx
        pop cx
        mov ax, -1
        stc
        ret

;;; -----------------------------------------------------------------------
;;; fd_pos_to_sector: Convert fd_pos to absolute sector + byte offset
;;; Input:  SI = FD entry pointer
;;; Output: AX = absolute sector number, BX = byte offset within sector
;;; -----------------------------------------------------------------------
fd_pos_to_sector:
        mov ax, [si+FD_OFFSET_POSITION+2]
        mov bx, [si+FD_OFFSET_POSITION]
        ;; AX:BX >> 9 = sector offset
        shl ax, 7
        push cx
        mov cx, bx
        shr cx, 9
        or ax, cx
        pop cx
        add ax, [si+FD_OFFSET_START]
        ;; BX = pos & 0x1FF
        and bx, 01FFh
        ret

;;; -----------------------------------------------------------------------
;;; fd_read: Dispatch read to the backend for this fd type
;;; -----------------------------------------------------------------------
fd_read:
        call fd_lookup
        jc .read_err
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        je .to_console
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_DIRECTORY
        je .to_dir
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_FILE
        je .to_file
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_NET
        je .to_net
        .read_err:
        mov ax, -1
        stc
        ret
        .to_console: jmp fd_read_console
        .to_dir:     jmp fd_read_dir
        .to_file:    jmp fd_read_file
        .to_net:     jmp fd_read_net

;;; -----------------------------------------------------------------------
;;; fd_write: Dispatch write to the backend for this fd type
;;; -----------------------------------------------------------------------
fd_write:
        mov [fd_write_buffer], si  ; save user buffer before fd_lookup clobbers SI
        call fd_lookup
        jc .wr_err
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_CONSOLE
        je .to_console
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_FILE
        je .to_file
        cmp byte [si+FD_OFFSET_TYPE], FD_TYPE_NET
        je .to_net
        .wr_err:
        mov ax, -1
        stc
        ret
        .to_console: jmp fd_write_console
        .to_file:    jmp fd_write_file
        .to_net:     jmp fd_write_net

        ;; Variables
        fd_open_fd    dw 0
        fd_open_flags db 0
        fd_open_mode  db 0
        fd_open_name  dw 0
        fd_open_sector dw 0
        fd_table times FD_MAX * FD_ENTRY_SIZE db 0
        fd_write_buffer dw 0

%include "fd/console.asm"
%include "fd/file.asm"
%include "fd/net.asm"
