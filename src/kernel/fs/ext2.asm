;;; fs/ext2.asm -- ext2 filesystem VFS backend (read-only, 1 KB blocks only)
;;;
;;; VFS interface (called through vfs.asm function pointers):
;;; ext2_find:     SI=path → vfs_found_*, CF if not found
;;; ext2_init:     → CF if ext2 not detected; initialises state on success
;;; ext2_load:     DI=dest → CF on disk error
;;; ext2_read_sec: SI=fd_entry → SECTOR_BUFFER filled, BX=byte offset; CF on err
;;; ext2_readonly: → CF set (stub for unsupported write operations)
;;;
;;; Internal helpers:
;;; ext2_get_data_block: AX=block-index, BX=inode-ptr; AX=block-num, CF=err
;;; ext2_names_match:    SI=search-name, DI=entry-name, CX=entry-namelen; CF=no-match
;;; ext2_read_blk_sec:   AX=block, BX=sector-within-block; reads into SECTOR_BUFFER
;;; ext2_read_inode:     AX=inode-number; BX=pointer into SECTOR_BUFFER
;;; ext2_search_dir:     AX=dir-inode, SI=name; AX=found-inode, CF=not-found

;;; Superblock field offsets (all within the first 512-byte sector of block 1)
%assign EXT2_SB_FIRST_DATA_BLOCK  20
%assign EXT2_SB_LOG_BLOCK_SIZE    24
%assign EXT2_SB_INODES_PER_GROUP  40
%assign EXT2_SB_MAGIC             56
%assign EXT2_SB_REV_LEVEL         76
%assign EXT2_SB_INODE_SIZE        88
%assign EXT2_MAGIC                0EF53h

;;; Block group descriptor field offsets
%assign EXT2_BGD_INODE_TABLE      8

;;; Inode field offsets
%assign EXT2_INODE_MODE           0
%assign EXT2_INODE_SIZE_LO        4
%assign EXT2_INODE_BLOCK          40

;;; Directory entry field offsets
%assign EXT2_DIRENT_INODE         0
%assign EXT2_DIRENT_REC_LEN       4
%assign EXT2_DIRENT_NAME_LEN      6
%assign EXT2_DIRENT_NAME          8

;;; i_mode bits
%assign EXT2_S_IFDIR              04000h  ; directory
%assign EXT2_S_IXUSR              00100h  ; owner execute

%assign EXT2_ROOT_INODE           2

ext2_find:
        ;; Find a file (or "." root) and populate vfs_found_*
        ;; Input:  SI = null-terminated path (one optional '/')
        ;; Output: CF clear + vfs_found_* set; CF set if not found
        push bx
        push cx
        push dx
        push si
        push di
        ;; Handle "." — synthesise root directory entry
        cmp byte [si], '.'
        jne .ef_normal
        cmp byte [si+1], 0
        jne .ef_normal
        mov word [vfs_found_inode], EXT2_ROOT_INODE
        mov word [vfs_found_size], 0
        mov word [vfs_found_size+2], 0
        mov byte [vfs_found_mode], FLAG_DIRECTORY
        mov byte [vfs_found_type], FD_TYPE_DIRECTORY
        mov word [vfs_found_dir_sec], 0
        mov word [vfs_found_dir_off], 0
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .ef_normal:
        ;; Scan for '/'
        mov di, si
        .ef_slash:
        mov al, [di]
        test al, al
        jz .ef_no_slash
        cmp al, '/'
        je .ef_has_slash
        inc di
        jmp .ef_slash
        .ef_no_slash:
        ;; Simple name: search root directory
        mov ax, EXT2_ROOT_INODE
        call ext2_search_dir    ; AX = found inode, CF if not found
        jc .ef_not_found
        jmp .ef_got_inode
        .ef_has_slash:
        ;; Split at '/': find dir component in root, then file in that dir
        mov byte [di], 0
        push di
        mov ax, EXT2_ROOT_INODE
        call ext2_search_dir    ; AX = dir inode
        pop di
        mov byte [di], '/'
        jc .ef_not_found
        inc di                  ; DI = basename (past '/')
        mov si, di
        call ext2_search_dir    ; AX = file inode
        jc .ef_not_found
        .ef_got_inode:
        ;; AX = inode number; read inode to get size and mode
        mov [vfs_found_inode], ax
        call ext2_read_inode    ; BX = pointer to inode in SECTOR_BUFFER
        mov cx, [bx+EXT2_INODE_MODE]
        mov dx, [bx+EXT2_INODE_SIZE_LO]
        mov [vfs_found_size], dx
        mov word [vfs_found_size+2], 0
        mov word [vfs_found_dir_sec], 0
        mov word [vfs_found_dir_off], 0
        ;; Determine type and mode flags from i_mode
        mov byte [vfs_found_mode], 0
        mov byte [vfs_found_type], FD_TYPE_FILE
        test cx, EXT2_S_IFDIR
        jz .ef_check_exec
        mov byte [vfs_found_type], FD_TYPE_DIRECTORY
        or byte [vfs_found_mode], FLAG_DIRECTORY
        .ef_check_exec:
        test cx, EXT2_S_IXUSR
        jz .ef_done
        or byte [vfs_found_mode], FLAG_EXECUTE
        .ef_done:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .ef_not_found:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_init:
        ;; Detect ext2 and initialise state.  Only 1 KB blocks are supported.
        ;; Input:  (none)
        ;; Output: CF clear on success; CF set if not ext2 or unsupported geometry
        push ax
        push bx
        push cx
        ;; Superblock is at byte 1024 from partition start = sector EXT2_START_SECTOR+2
        mov ax, EXT2_START_SECTOR + 2
        call read_sector
        jc .ei_err
        cmp word [SECTOR_BUFFER+EXT2_SB_MAGIC], EXT2_MAGIC
        jne .ei_err
        ;; Only 1 KB blocks (s_log_block_size == 0)
        cmp word [SECTOR_BUFFER+EXT2_SB_LOG_BLOCK_SIZE], 0
        jne .ei_err
        mov byte [ext2_log_block_size], 0
        mov ax, [SECTOR_BUFFER+EXT2_SB_INODES_PER_GROUP]
        mov [ext2_inodes_per_group], ax
        ;; Inode size: 128 for rev 0, read from superblock for rev 1+
        mov word [ext2_inode_size], 128
        cmp word [SECTOR_BUFFER+EXT2_SB_REV_LEVEL], 0
        je .ei_read_bgd
        mov ax, [SECTOR_BUFFER+EXT2_SB_INODE_SIZE]
        mov [ext2_inode_size], ax
        .ei_read_bgd:
        ;; Block group descriptor table is at block 2 (for 1 KB blocks)
        ;; = sector EXT2_START_SECTOR+4
        mov ax, EXT2_START_SECTOR + 4
        call read_sector
        jc .ei_err
        mov ax, [SECTOR_BUFFER+EXT2_BGD_INODE_TABLE]
        mov [ext2_inode_table_blk], ax
        pop cx
        pop bx
        pop ax
        clc
        ret
        .ei_err:
        pop cx
        pop bx
        pop ax
        stc
        ret

ext2_load:
        ;; Load file data into memory using vfs_found_inode and vfs_found_size.
        ;; Supports direct blocks (0..11) and the singly-indirect block (i_block[12]).
        ;; Input:  DI = destination address
        ;; Output: CF set on disk error
        push bx
        push cx
        push si
        ;; Read inode into SECTOR_BUFFER; save 12 direct block numbers + indirect ptr
        mov ax, [vfs_found_inode]
        call ext2_read_inode            ; BX = pointer to inode
        mov si, bx
        add si, EXT2_INODE_BLOCK
        push di
        mov di, ext2_load_blks
        mov cx, 12
        .el_save:
        mov ax, [si]                    ; low 16 bits of each 32-bit block ptr
        stosw
        add si, 4
        dec cx
        jnz .el_save
        mov ax, [si]                    ; i_block[12] = singly-indirect block pointer
        mov [ext2_load_indirect_ptr], ax
        pop di
        mov cx, [vfs_found_size]        ; remaining bytes (low 16 bits)
        mov word [ext2_load_rem], cx
        mov word [ext2_load_blk_counter], 0
        ;; Main loop: iterate block_counter from 0; direct (0..11) then indirect (12+)
        .el_block:
        mov cx, [ext2_load_rem]
        test cx, cx
        jbe .el_done
        mov ax, [ext2_load_blk_counter]
        cmp ax, 12
        jb .el_direct
        ;; Singly indirect: look up entry in i_block[12]
        sub ax, 12                      ; indirect_idx (0-based within indirect block)
        mov cx, ax
        shr cx, 7                       ; CX = sector within indirect block (0 or 1)
        and ax, 07Fh
        shl ax, 2                       ; AX = byte offset of entry within that sector
        push ax                         ; save entry_offset
        mov bx, cx
        mov ax, [ext2_load_indirect_ptr]
        test ax, ax
        jz .el_done_pop
        call ext2_read_blk_sec          ; AX=indirect_ptr, BX=sector_in_ind → SECTOR_BUFFER
        jc .el_err_pop
        pop bx                          ; BX = entry_offset
        mov ax, [SECTOR_BUFFER + bx]    ; AX = data block number
        jmp .el_got_block
        .el_done_pop:
        add sp, 2
        jmp .el_done
        .el_err_pop:
        add sp, 2
        jmp .el_err
        .el_direct:
        shl ax, 1                       ; index * 2 (word-sized entries in ext2_load_blks)
        mov bx, ax
        mov ax, [ext2_load_blks + bx]
        .el_got_block:
        test ax, ax
        jz .el_done                     ; zero block pointer = end
        ;; Read 2 sectors (1 KB block)
        xor bx, bx
        .el_sector:
        push ax
        push bx
        call ext2_read_blk_sec          ; AX=block, BX=sector offset
        pop bx
        pop ax
        jc .el_err
        ;; Copy min(512, remaining) bytes from SECTOR_BUFFER to DI
        mov cx, [ext2_load_rem]
        push cx
        cmp cx, 512
        jbe .el_partial
        mov cx, 256                     ; full sector = 256 words
        jmp .el_copy
        .el_partial:
        inc cx
        shr cx, 1
        .el_copy:
        push si
        mov si, SECTOR_BUFFER
        cld
        rep movsw
        pop si
        pop cx
        ;; Subtract 512 from remaining
        sub cx, 512
        jbe .el_done
        mov [ext2_load_rem], cx
        inc bx
        cmp bx, 2                       ; 2 sectors per 1 KB block
        jb .el_sector
        inc word [ext2_load_blk_counter]
        jmp .el_block
        .el_done:
        pop si
        pop cx
        pop bx
        clc
        ret
        .el_err:
        pop si
        pop cx
        pop bx
        stc
        ret

ext2_readonly:
        ;; Stub for write operations not supported on ext2 (read-only)
        stc
        ret

;;; -----------------------------------------------------------------------
;;; Internal helpers
;;; -----------------------------------------------------------------------

ext2_get_data_block:
        ;; Translate a logical block index to an ext2 block number.
        ;; Must be called immediately after ext2_read_inode (SECTOR_BUFFER holds the inode sector).
        ;; Input:  AX = block_index, BX = inode pointer in SECTOR_BUFFER
        ;; Output: AX = ext2 block number; CF on disk error (only possible for indirect)
        ;; Clobbers: AX, BX, CX
        cmp ax, 12
        jb .direct
        ;; Singly indirect: i_block[12] → indirect block → data block
        sub ax, 12                      ; indirect_idx (0-based)
        mov cx, ax
        and cx, 07Fh
        shl cx, 2                       ; CX = byte offset of entry within sector
        shr ax, 7                       ; AX = sector within indirect block (0 or 1)
        push cx                         ; save entry_offset
        add bx, EXT2_INODE_BLOCK + 48   ; BX = &i_block[12] (12 * 4 = 48)
        mov cx, [bx]                    ; CX = indirect block pointer (16-bit)
        mov bx, ax                      ; BX = sector_in_indirect_block
        mov ax, cx                      ; AX = indirect block pointer
        call ext2_read_blk_sec          ; AX=indirect_ptr, BX=sector_in_ind → SECTOR_BUFFER
        jc .indirect_err
        pop bx                          ; BX = entry_offset
        mov ax, [SECTOR_BUFFER + bx]    ; AX = data block number
        clc
        ret
        .indirect_err:
        add sp, 2                       ; discard entry_offset
        stc
        ret
        .direct:
        shl ax, 2                       ; AX = block_index * 4
        add bx, EXT2_INODE_BLOCK        ; BX = &i_block[0]
        add bx, ax                      ; BX = &i_block[block_index]
        mov ax, [bx]                    ; AX = block number
        clc
        ret

ext2_names_match:
        ;; Compare null-terminated SI against entry name at DI with length CL
        ;; Output: CF clear = match, CF set = no match
        ;; Preserves all registers
        push ax
        push bx
        push cx
        push si
        push di
        ;; Compute strlen(SI) into BX using [si+bx] to avoid modifying SI
        xor bx, bx
        .enm_len:
        cmp byte [si+bx], 0
        je .enm_len_done
        inc bx
        jmp .enm_len
        .enm_len_done:
        xor ch, ch                      ; CX = entry name length (CL already set)
        cmp bx, cx
        jne .enm_no_match
        test cx, cx
        jz .enm_match                   ; both empty
        repe cmpsb
        jne .enm_no_match
        .enm_match:
        pop di
        pop si
        pop cx
        pop bx
        pop ax
        clc
        ret
        .enm_no_match:
        pop di
        pop si
        pop cx
        pop bx
        pop ax
        stc
        ret

ext2_read_blk_sec:
        ;; Read one 512-byte sector from an ext2 block into SECTOR_BUFFER
        ;; Input: AX = block number, BX = sector offset within block (0 or 1 for 1 KB)
        ;; Output: CF set on error
        ;; Clobbers: AX
        push cx
        xor cx, cx
        mov cl, [ext2_log_block_size]
        inc cl                          ; sectors_per_block = 2^(log+1)
        shl ax, cl                      ; AX = first disk sector of block (relative)
        add ax, EXT2_START_SECTOR
        add ax, bx
        call read_sector
        pop cx
        ret

ext2_read_inode:
        ;; Read inode AX into SECTOR_BUFFER; return BX = pointer to inode
        ;; Clobbers: AX, BX, CX, DX
        push si
        dec ax                          ; 0-based index
        ;; byte_offset = index * inode_size (assume inode_size divides 512)
        xor dx, dx
        mul word [ext2_inode_size]      ; DX:AX = byte offset into inode table
        ;; sector_within_block = byte_offset / 512
        ;; For 128-byte inodes: 4 per sector, so sector = AX >> 7 >> 2 = AX >> 9? no:
        ;; 512 bytes / sector; byte_offset / 512 = AX >> 9 (high bits from DX << 7)
        mov bx, ax
        and bx, 01FFh                   ; bx = byte offset within sector
        push bx
        shr ax, 9
        shl dx, 7
        or ax, dx                       ; AX = sector within inode table block
        mov bx, ax
        mov ax, [ext2_inode_table_blk]
        call ext2_read_blk_sec          ; AX=block, BX=sector-in-block
        pop bx                          ; BX = byte offset within sector
        add bx, SECTOR_BUFFER
        pop si
        ret

ext2_search_dir:
        ;; Search directory inode AX for entry named SI
        ;; Output: AX = found inode, CF if not found
        push bx
        push cx
        push dx
        push si
        push di
        mov [ext2_sd_name], si
        ;; Read directory inode; save direct block pointers
        call ext2_read_inode            ; BX = pointer to inode
        mov si, bx
        add si, EXT2_INODE_BLOCK
        mov di, ext2_dir_blks
        mov cx, 12
        .esd_save:
        mov ax, [si]
        stosw
        add si, 4
        dec cx
        jnz .esd_save
        ;; Search each direct block
        mov bx, ext2_dir_blks           ; BX = pointer into block list
        .esd_next_blk:
        mov ax, [bx]
        add bx, 2
        test ax, ax
        jz .esd_not_found
        push bx
        call ext2_search_blk            ; AX=block, SI restored from ext2_sd_name
        pop bx
        jnc .esd_found
        cmp bx, ext2_dir_blks + 24      ; past the 12th entry?
        jb .esd_next_blk
        .esd_not_found:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret
        .esd_found:
        ;; AX = found inode
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret

ext2_search_blk:
        ;; Search all sectors of ext2 directory block AX for entry named [ext2_sd_name]
        ;; Output: AX = inode if found, CF if not found
        push bx
        push cx
        push dx
        push si
        push di
        ;; sectors_per_block = 2 for 1 KB blocks (ext2_log_block_size = 0)
        mov dx, 2                       ; DX = sectors_per_block (hardcoded for 1 KB)
        xor cx, cx                      ; CX = sector within block
        .esb_sector:
        cmp cx, dx
        jae .esb_not_found
        push ax
        push cx
        push dx
        mov bx, cx
        call ext2_read_blk_sec
        pop dx
        pop cx
        pop ax
        jc .esb_not_found
        ;; Scan directory entries in SECTOR_BUFFER
        mov si, [ext2_sd_name]
        mov di, SECTOR_BUFFER
        .esb_entry:
        ;; Bounds check
        mov bx, di
        sub bx, SECTOR_BUFFER
        cmp bx, 512
        jae .esb_next_sector
        ;; Validate rec_len
        mov bx, [di+EXT2_DIRENT_REC_LEN]
        cmp bx, EXT2_DIRENT_NAME        ; minimum 8 bytes
        jb .esb_next_sector
        ;; Skip deleted entries (inode = 0)
        mov ax, [di+EXT2_DIRENT_INODE]
        test ax, ax
        jz .esb_advance
        ;; Compare name
        push ax                         ; save inode
        push bx                         ; save rec_len
        push di
        add di, EXT2_DIRENT_NAME
        mov cl, [di-2]                  ; name_len is at EXT2_DIRENT_NAME_LEN = offset 6
                                        ; di now points to name (offset 8), so name_len
                                        ; is at [di-2]
        call ext2_names_match           ; SI=search, DI=entry name, CL=namelen; CF=no match
        pop di
        pop bx                          ; rec_len
        pop ax                          ; inode
        jc .esb_advance
        ;; Found!
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .esb_advance:
        add di, bx                      ; advance by rec_len
        jmp .esb_entry
        .esb_next_sector:
        inc cx
        jmp .esb_sector
        .esb_not_found:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_read_sec:
        ;; Fill SECTOR_BUFFER with the 512-byte sector at the current read position.
        ;; Handles direct blocks (0..11) and the singly-indirect block via ext2_get_data_block.
        ;; Input:  SI = FD entry pointer (FD_OFFSET_START = inode number)
        ;; Output: SECTOR_BUFFER filled, BX = byte offset within sector; CF on error
        push ax
        push cx
        push dx
        ;; Decompose 32-bit position: block_index = pos >> 10; sector_in_block = (pos >> 9) & 1
        mov ax, [si+FD_OFFSET_POSITION]
        mov dx, [si+FD_OFFSET_POSITION+2]
        mov bx, ax
        and bx, 01FFh           ; BX = byte offset within sector (to return)
        mov cx, ax
        shr cx, 9
        and cx, 1               ; CX = sector_in_block (0 or 1)
        shr ax, 10              ; low 6 bits of block_index
        shl dx, 6               ; high bits of block_index
        or ax, dx               ; AX = block_index
        push bx                 ; save byte offset
        push cx                 ; save sector_in_block
        push ax                 ; save block_index
        mov ax, [si+FD_OFFSET_START]    ; inode number
        call ext2_read_inode            ; BX = &inode in SECTOR_BUFFER; clobbers AX,CX,DX
        pop ax                          ; AX = block_index
        call ext2_get_data_block        ; AX=block_index, BX=inode_ptr → AX=block_num; CF on err
        jc .err
        pop cx                          ; CX = sector_in_block
        pop bx                          ; BX = byte offset
        push bx                         ; re-save byte offset
        mov bx, cx
        call ext2_read_blk_sec          ; AX = block, BX = sector_in_block → SECTOR_BUFFER
        pop bx                          ; BX = byte offset within sector (to return)
        jc .blk_err
        pop dx
        pop cx
        pop ax
        ret
        .err:                           ; ext2_get_data_block failed; discard sector_in_block + byte_offset
        add sp, 4
        .blk_err:                       ; ext2_read_blk_sec failed; outer regs still on stack
        pop dx
        pop cx
        pop ax
        stc
        ret

        ;; State
        ext2_dir_blks        times 12 dw 0
        ext2_inode_size      dw 128
        ext2_inode_table_blk dw 0
        ext2_inodes_per_group dw 0
        ext2_load_blk_counter  dw 0
        ext2_load_blks         times 12 dw 0
        ext2_load_indirect_ptr dw 0
        ext2_load_rem          dw 0
        ext2_log_block_size  db 0
        ext2_sd_name         dw 0
