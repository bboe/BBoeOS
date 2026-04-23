;;; fs/ext2.asm -- ext2 filesystem VFS backend
;;;
;;; VFS interface (called through vfs.asm function pointers):
;;; ext2_chmod:    SI=path, AL=mode → CF on error, AL=error code
;;; ext2_delete:   SI=path → CF on error, AL=error code
;;; ext2_find:     SI=path → vfs_found_*, CF if not found
;;; ext2_rmdir:    SI=path → CF on error, AL=error code
;;; ext2_init:     → CF if ext2 not detected; initialises state on success
;;; ext2_load:     DI=dest → CF on disk error
;;; ext2_mkdir:    SI=path → AX=inode, CF on error, AL=error code
;;; ext2_read_dir: SI=fd_entry, DI=buf → AX=bytes (DIRECTORY_ENTRY_SIZE or 0); CF on err
;;; ext2_read_sec: SI=fd_entry → SECTOR_BUFFER filled, BX=byte offset; CF on err
;;; ext2_rename:   SI=old path, DI=new path → CF on error, AL=error code
;;;
;;; Internal helpers:
;;; ext2_bgd_block_alloc:   dec bg_free_blocks_count in BGD+SB; Clobbers AX,BX,CX,DX
;;; ext2_bgd_block_free:    inc bg_free_blocks_count in BGD+SB; Clobbers AX,BX,CX,DX
;;; ext2_bgd_dir_alloc:     inc bg_used_dirs_count in BGD; Clobbers AX,BX,CX,DX
;;; ext2_bgd_dir_free:      dec bg_used_dirs_count in BGD; Clobbers AX,BX,CX,DX
;;; ext2_bgd_inode_alloc:   dec bg_free_inodes_count in BGD+SB; Clobbers AX,BX,CX,DX
;;; ext2_bgd_inode_free:    inc bg_free_inodes_count in BGD+SB; Clobbers AX,BX,CX,DX
;;; ext2_free_bit:          AX=bitmap-block, BX=bit-index; CF on error
;;; ext2_free_block:        AX=block-number; CF on error
;;; ext2_free_inode:        AX=inode-number (1-based); CF on error
;;; ext2_get_data_block:    AX=block-index, BX=inode-ptr; AX=block-num, CF=err
;;; ext2_names_match:       SI=search-name, DI=entry-name, CX=entry-namelen; CF=no-match
;;; ext2_read_blk_sec:      AX=block, BX=sector-within-block; reads into SECTOR_BUFFER
;;; ext2_read_inode:        AX=inode-number; BX=pointer into SECTOR_BUFFER
;;; ext2_check_dir_empty:   AX=block; CF if non-dot entry found
;;; ext2_remove_dir_entry:  AX=dir-inode, SI=name; CF on error
;;; ext2_resolve_path:      SI=path → AX=parent-inode, SI=basename; CF if parent not found
;;; ext2_search_dir:        AX=dir-inode, SI=name; AX=found-inode, CF=not-found

;;; Superblock field offsets (all within the first 512-byte sector of block 1)
%assign EXT2_SB_FIRST_DATA_BLOCK  20
%assign EXT2_SB_LOG_BLOCK_SIZE    24
%assign EXT2_SB_INODES_PER_GROUP  40
%assign EXT2_SB_MAGIC             56
%assign EXT2_SB_REV_LEVEL         76
%assign EXT2_SB_INODE_SIZE        88
%assign EXT2_MAGIC                0EF53h

;;; Block group descriptor field offsets
%assign EXT2_BGD_BLOCK_BITMAP       0
%assign EXT2_BGD_INODE_BITMAP       4
%assign EXT2_BGD_INODE_TABLE        8
%assign EXT2_BGD_FREE_BLOCKS_COUNT  12
%assign EXT2_BGD_FREE_INODES_COUNT  14
%assign EXT2_BGD_USED_DIRS_COUNT    16

;;; Superblock free-count field offsets (low 16 bits, high 16 bits always 0 for small FSes)
%assign EXT2_SB_FREE_BLOCKS_COUNT   12
%assign EXT2_SB_FREE_INODES_COUNT   16

;;; Inode field offsets
%assign EXT2_INODE_ATIME          8
%assign EXT2_INODE_BLOCK          40
%assign EXT2_INODE_BLOCKS         28
%assign EXT2_INODE_CTIME          12
%assign EXT2_INODE_DTIME          20
%assign EXT2_INODE_LINKS_COUNT    26
%assign EXT2_INODE_MODE           0
%assign EXT2_INODE_MTIME          16
%assign EXT2_INODE_SIZE_LO        4

;;; i_mode value for a new regular file (S_IFREG | 0644)
%assign EXT2_S_IFREG              08000h

;;; Directory entry field offsets
%assign EXT2_DIRENT_INODE         0
%assign EXT2_DIRENT_REC_LEN       4
%assign EXT2_DIRENT_NAME_LEN      6
%assign EXT2_DIRENT_NAME          8

;;; i_mode bits
%assign EXT2_S_IFDIR              04000h  ; directory
%assign EXT2_S_IXUSR              00100h  ; owner execute
%assign EXT2_S_IXALL              (EXT2_S_IXUSR | 00040h | 00010h)  ; a+x

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
        ;; Handle "." — synthesise root directory entry using actual inode size
        cmp byte [si], '.'
        jne .ef_normal
        cmp byte [si+1], 0
        jne .ef_normal
        mov word [vfs_found_inode], EXT2_ROOT_INODE
        mov ax, EXT2_ROOT_INODE
        call ext2_read_inode            ; BX = pointer to inode in SECTOR_BUFFER
        mov cx, [bx+EXT2_INODE_SIZE_LO]
        mov [vfs_found_size], cx
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
        ;; Detect ext2 and initialise state.  1 KB, 2 KB, and 4 KB blocks supported.
        ;; Input:  (none)
        ;; Output: CF clear on success; CF set if not ext2
        push ax
        push bx
        push cx
        ;; Superblock is at byte 1024 from partition start = sector EXT2_START_SECTOR+2
        mov ax, EXT2_START_SECTOR + 2
        call read_sector
        jc .ei_err
        cmp word [SECTOR_BUFFER+EXT2_SB_MAGIC], EXT2_MAGIC
        jne .ei_err
        ;; s_log_block_size: 0=1KB, 1=2KB, 2=4KB
        mov al, [SECTOR_BUFFER+EXT2_SB_LOG_BLOCK_SIZE]
        mov [ext2_log_block_size], al
        mov ax, [SECTOR_BUFFER+EXT2_SB_FIRST_DATA_BLOCK]
        mov [ext2_first_data_block], ax
        mov ax, [SECTOR_BUFFER+EXT2_SB_INODES_PER_GROUP]
        mov [ext2_inodes_per_group], ax
        ;; Inode size: 128 for rev 0, read from superblock for rev 1+
        mov word [ext2_inode_size], 128
        cmp word [SECTOR_BUFFER+EXT2_SB_REV_LEVEL], 0
        je .ei_read_bgd
        mov ax, [SECTOR_BUFFER+EXT2_SB_INODE_SIZE]
        mov [ext2_inode_size], ax
        .ei_read_bgd:
        ;; Block group descriptor table: block 2 for 1 KB blocks, block 1 for 2/4 KB
        cmp byte [ext2_log_block_size], 0
        je .ei_bgd_blk2
        mov ax, 1
        jmp .ei_bgd_read
        .ei_bgd_blk2:
        mov ax, 2
        .ei_bgd_read:
        mov [ext2_bgd_block], ax        ; save for ext2_bgd_* helpers
        xor bx, bx
        call ext2_read_blk_sec          ; AX=bgd_block, BX=0 → SECTOR_BUFFER
        jc .ei_err
        mov ax, [SECTOR_BUFFER+EXT2_BGD_BLOCK_BITMAP]
        mov [ext2_block_bitmap_blk], ax
        mov ax, [SECTOR_BUFFER+EXT2_BGD_INODE_BITMAP]
        mov [ext2_inode_bitmap_blk], ax
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
        ;; Supports direct (0..11), singly-indirect (i_block[12]), doubly-indirect (i_block[13]).
        ;; Input:  DI = destination address
        ;; Output: CF set on disk error
        push bx
        push cx
        push si
        ;; Read inode; save 12 direct block numbers, indirect ptr, doubly-indirect ptr
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
        add si, 4
        mov ax, [si]                    ; i_block[13] = doubly-indirect block pointer
        mov [ext2_load_dbl_ptr], ax
        ;; ptrs_per_blk = 256 << log_block_size
        xor cx, cx
        mov cl, [ext2_log_block_size]
        mov ax, 256
        shl ax, cl
        mov [ext2_load_ptrs], ax
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
        sub ax, 12                      ; AX = idx within indirect region
        cmp ax, [ext2_load_ptrs]
        jae .el_doubly
        ;; --- Singly indirect ---
        mov cx, ax
        shr cx, 7                       ; CX = sector within indirect block
        and ax, 07Fh
        shl ax, 2                       ; AX = byte offset of entry within sector
        push ax
        mov bx, cx
        mov ax, [ext2_load_indirect_ptr]
        test ax, ax
        jz .el_done_pop
        call ext2_read_blk_sec
        jc .el_err_pop
        pop bx
        mov ax, [SECTOR_BUFFER + bx]
        jmp .el_got_block
        .el_done_pop:
        add sp, 2
        jmp .el_done
        .el_err_pop:
        add sp, 2
        jmp .el_err
        ;; --- Doubly indirect ---
        .el_doubly:
        sub ax, [ext2_load_ptrs]        ; AX = dbl_idx
        xor cx, cx
        mov cl, [ext2_log_block_size]
        add cl, 8                       ; CL = log2(ptrs_per_blk)
        mov bx, ax
        shr bx, cl                      ; BX = outer_idx
        mov cx, [ext2_load_ptrs]
        dec cx
        and ax, cx                      ; AX = inner_idx
        push ax                         ; save inner_idx
        ;; Outer lookup: sector = outer_idx >> 7, offset = (outer_idx & 0x7F) * 4
        mov cx, bx
        shr cx, 7
        and bx, 07Fh
        shl bx, 2
        push bx                         ; save outer byte offset
        mov bx, cx
        mov ax, [ext2_load_dbl_ptr]
        test ax, ax
        jz .el_done_pop2
        call ext2_read_blk_sec
        jc .el_err_pop2
        pop bx
        mov cx, [SECTOR_BUFFER + bx]    ; CX = singly-indirect block number
        ;; Inner lookup
        pop ax                          ; AX = inner_idx
        mov bx, ax
        shr bx, 7
        and ax, 07Fh
        shl ax, 2
        push ax
        mov ax, cx
        call ext2_read_blk_sec
        jc .el_err_pop
        pop bx
        mov ax, [SECTOR_BUFFER + bx]
        jmp .el_got_block
        .el_done_pop2:
        add sp, 4
        jmp .el_done
        .el_err_pop2:
        add sp, 4
        jmp .el_err
        .el_direct:
        shl ax, 1                       ; index * 2 (word-sized entries in ext2_load_blks)
        mov bx, ax
        mov ax, [ext2_load_blks + bx]
        .el_got_block:
        test ax, ax
        jz .el_done                     ; zero block pointer = end
        ;; Read all sectors of the block
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
        ;; Continue until all sectors_per_block = 1 << (log+1) have been read
        push cx
        push ax
        xor ch, ch
        mov cl, [ext2_log_block_size]
        inc cl
        mov ax, 1
        shl ax, cl                      ; AX = sectors_per_block
        cmp bx, ax
        pop ax
        pop cx
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

ext2_mkdir:
        ;; Create a new subdirectory under the given parent path.
        ;; Input:  SI = path (e.g. "mydir" or "parent/child")
        ;; Output: AX = new inode number; CF on error, AL = error code
        push bx
        push cx
        push dx
        push si
        push di
        mov [ext2_mk_name], si
        ;; Reject if name already exists
        call ext2_find
        jc .emkdir_ok
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        mov al, ERROR_EXISTS
        stc
        ret
        .emkdir_ok:
        ;; Resolve parent directory and basename from path
        mov si, [ext2_mk_name]
        call ext2_resolve_path          ; AX = parent inode, SI = basename; CF if not found
        jc .emkdir_err
        mov [ext2_mk_parent_inode], ax
        mov [ext2_mk_name], si          ; narrow to basename only
        ;; Allocate inode for the new directory
        call ext2_alloc_inode           ; AX = inode number, CF on err
        jc .emkdir_err
        mov [ext2_mk_new_inode], ax
        ;; Allocate data block for the directory contents
        call ext2_alloc_block           ; AX = block number, CF on err
        jc .emkdir_err
        mov [ext2_mk_new_blk], ax
        ;; Read the new inode, zero it, then set fields
        mov ax, [ext2_mk_new_inode]
        call ext2_read_inode            ; BX = inode ptr in SECTOR_BUFFER
        push si
        push cx
        mov si, bx
        mov cx, [ext2_inode_size]
        xor ax, ax
        cld
        .emkdir_zero_inode:
        mov [si], ax
        add si, 2
        sub cx, 2
        jnz .emkdir_zero_inode
        pop cx
        pop si
        mov word [bx + EXT2_INODE_MODE], EXT2_S_IFDIR | 01EDh  ; S_IFDIR | 0755
        mov word [bx + EXT2_INODE_LINKS_COUNT], 2
        ;; inode size = block_size = 1024 << log_block_size
        xor ah, ah
        mov al, [ext2_log_block_size]
        mov cl, al
        mov ax, 1024
        shl ax, cl
        mov [bx + EXT2_INODE_SIZE_LO], ax
        mov word [bx + EXT2_INODE_SIZE_LO + 2], 0
        mov ax, [ext2_mk_new_blk]
        mov [bx + EXT2_INODE_BLOCK], ax
        mov word [bx + EXT2_INODE_BLOCK + 2], 0
        ;; i_blocks = sectors_per_block (one data block allocated)
        xor cx, cx
        mov cl, [ext2_log_block_size]
        inc cl
        mov ax, 1
        shl ax, cl                      ; AX = sectors_per_block
        mov [bx + EXT2_INODE_BLOCKS], ax
        mov word [bx + EXT2_INODE_BLOCKS + 2], 0
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .emkdir_err
        ;; Build '.' and '..' entries in SECTOR_BUFFER; write to block sector 0
        push di
        mov di, SECTOR_BUFFER
        mov cx, 256
        xor ax, ax
        cld
        rep stosw
        pop di
        mov ax, [ext2_mk_new_inode]
        mov [SECTOR_BUFFER + EXT2_DIRENT_INODE], ax
        mov word [SECTOR_BUFFER + EXT2_DIRENT_INODE + 2], 0
        mov word [SECTOR_BUFFER + EXT2_DIRENT_REC_LEN], 12
        mov byte [SECTOR_BUFFER + EXT2_DIRENT_NAME_LEN], 1
        mov byte [SECTOR_BUFFER + EXT2_DIRENT_NAME_LEN + 1], 2  ; FT_DIR
        mov byte [SECTOR_BUFFER + EXT2_DIRENT_NAME], '.'
        mov ax, [ext2_mk_parent_inode]
        mov [SECTOR_BUFFER + 12 + EXT2_DIRENT_INODE], ax
        mov word [SECTOR_BUFFER + 12 + EXT2_DIRENT_INODE + 2], 0
        ;; '..' rec_len fills rest of block: block_size - 12
        xor ah, ah
        mov al, [ext2_log_block_size]
        mov cl, al
        mov ax, 1024
        shl ax, cl                      ; AX = block_size
        sub ax, 12                      ; AX = block_size - 12
        mov [SECTOR_BUFFER + 12 + EXT2_DIRENT_REC_LEN], ax
        mov byte [SECTOR_BUFFER + 12 + EXT2_DIRENT_NAME_LEN], 2
        mov byte [SECTOR_BUFFER + 12 + EXT2_DIRENT_NAME_LEN + 1], 2  ; FT_DIR
        mov byte [SECTOR_BUFFER + 12 + EXT2_DIRENT_NAME], '.'
        mov byte [SECTOR_BUFFER + 12 + EXT2_DIRENT_NAME + 1], '.'
        ;; Compute sector 0 of the new block; write it
        push cx
        xor cx, cx
        mov cl, [ext2_log_block_size]
        inc cl
        mov ax, [ext2_mk_new_blk]
        shl ax, cl
        add ax, EXT2_START_SECTOR
        mov [ext2_last_blk_sec], ax
        pop cx
        call write_sector
        jc .emkdir_err
        ;; Zero SECTOR_BUFFER and write sectors 1..sectors_per_block-1 of the new block
        push di
        mov di, SECTOR_BUFFER
        mov cx, 256
        xor ax, ax
        cld
        rep stosw
        pop di
        xor ah, ah
        mov al, [ext2_log_block_size]
        mov cl, al
        mov bx, 2
        shl bx, cl                      ; BX = sectors_per_block
        dec bx                          ; BX = count of remaining sectors
        .emkdir_zero_next:
        test bx, bx
        jz .emkdir_zeros_done
        inc word [ext2_last_blk_sec]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .emkdir_err
        dec bx
        jmp .emkdir_zero_next
        .emkdir_zeros_done:
        ;; Add entry for new directory in parent
        mov byte [ext2_ade_filetype], 2    ; FT_DIR
        mov ax, [ext2_mk_parent_inode]
        mov di, [ext2_mk_name]
        mov bx, [ext2_mk_new_inode]
        call ext2_add_dir_entry
        jc .emkdir_err
        ;; Increment parent's i_links_count (child's '..' back-link)
        mov ax, [ext2_mk_parent_inode]
        call ext2_read_inode            ; BX = parent inode ptr
        inc word [bx + EXT2_INODE_LINKS_COUNT]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .emkdir_err
        call ext2_bgd_dir_alloc
        mov ax, [ext2_mk_new_inode]
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .emkdir_err:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_read_dir:
        ;; Read the next non-empty ext2 directory entry in bbfs format into [DI]
        ;; SI = FD entry pointer (FD_OFFSET_START = inode number, FD_OFFSET_SIZE = dir data size)
        ;; DI = output buffer (DIRECTORY_ENTRY_SIZE bytes)
        ;; Returns AX = DIRECTORY_ENTRY_SIZE if found, 0 at EOF, CF on error
        ;;
        ;; Name is staged through ext2_rd_name (static buffer) because ext2_read_inode
        ;; clobbers SECTOR_BUFFER — which may alias the caller's output buffer DI.
        push bx
        push cx
        push dx
        push di
        mov [ext2_rd_outbuf], di        ; save for use after ext2_read_inode
        .erd_loop:
        ;; 32-bit EOF: pos >= size
        mov ax, [si+FD_OFFSET_POSITION+2]
        cmp ax, [si+FD_OFFSET_SIZE+2]
        ja .erd_eof
        jb .erd_not_eof
        mov ax, [si+FD_OFFSET_POSITION]
        cmp ax, [si+FD_OFFSET_SIZE]
        jae .erd_eof
        .erd_not_eof:
        call ext2_read_sec              ; SI=fd_entry → SECTOR_BUFFER filled, BX=byte offset
        jc .erd_err
        ;; rec_len (used to advance position)
        mov dx, [SECTOR_BUFFER + bx + EXT2_DIRENT_REC_LEN]
        cmp dx, EXT2_DIRENT_NAME        ; < 8 is invalid
        jb .erd_err
        ;; inode (low 16 bits)
        mov ax, [SECTOR_BUFFER + bx + EXT2_DIRENT_INODE]
        test ax, ax
        jz .erd_skip                    ; deleted entry: advance and retry
        ;; Save rec_len and inode across ext2_read_inode (which clobbers SECTOR_BUFFER)
        mov [ext2_rd_rec_len], dx
        mov [ext2_rd_inode], ax
        ;; Stage name into ext2_rd_name static buffer (safe across ext2_read_inode)
        push si
        mov cl, [SECTOR_BUFFER + bx + EXT2_DIRENT_NAME_LEN]
        xor ch, ch
        cmp cx, DIRECTORY_NAME_LENGTH - 1
        jbe .erd_namelen_ok
        mov cx, DIRECTORY_NAME_LENGTH - 1
        .erd_namelen_ok:
        mov si, SECTOR_BUFFER
        add si, bx
        add si, EXT2_DIRENT_NAME        ; SI = entry name in SECTOR_BUFFER
        mov di, ext2_rd_name
        cld
        rep movsb                       ; copy name bytes to static buffer
        mov byte [di], 0                ; null-terminate
        pop si                          ; restore fd_entry pointer
        ;; Read inode to get mode and size (clobbers SECTOR_BUFFER, AX, BX, CX, DX)
        mov ax, [ext2_rd_inode]
        call ext2_read_inode            ; BX = pointer to inode in SECTOR_BUFFER
        mov cx, [bx+EXT2_INODE_MODE]
        mov dx, [bx+EXT2_INODE_SIZE_LO] ; read size before writes to output
        ;; Compute flags
        xor al, al
        test cx, EXT2_S_IFDIR
        jz .erd_check_exec
        or al, FLAG_DIRECTORY
        jmp .erd_set_flags      ; directories are never also marked executable
        .erd_check_exec:
        test cx, EXT2_S_IXUSR
        jz .erd_set_flags
        or al, FLAG_EXECUTE
        .erd_set_flags:
        ;; Copy name from static buffer to output [DI+0..DI+24]
        push si
        mov di, [ext2_rd_outbuf]
        mov si, ext2_rd_name
        mov cx, DIRECTORY_NAME_LENGTH   ; copy all 25 bytes (name + null + padding)
        rep movsb
        pop si                          ; restore fd_entry pointer
        ;; Write flags, inode, size into output
        mov di, [ext2_rd_outbuf]
        mov [di+DIRECTORY_OFFSET_FLAGS], al
        mov ax, [ext2_rd_inode]
        mov [di+DIRECTORY_OFFSET_SECTOR], ax
        mov [di+DIRECTORY_OFFSET_SIZE], dx
        mov word [di+DIRECTORY_OFFSET_SIZE+2], 0
        ;; Advance position by rec_len
        mov ax, [ext2_rd_rec_len]
        add [si+FD_OFFSET_POSITION], ax
        adc word [si+FD_OFFSET_POSITION+2], 0
        mov ax, DIRECTORY_ENTRY_SIZE
        pop di
        pop dx
        pop cx
        pop bx
        clc
        ret
        .erd_skip:
        ;; Deleted entry: advance position by rec_len and retry
        add [si+FD_OFFSET_POSITION], dx
        adc word [si+FD_OFFSET_POSITION+2], 0
        jmp .erd_loop
        .erd_eof:
        pop di
        pop dx
        pop cx
        pop bx
        xor ax, ax
        clc
        ret
        .erd_err:
        pop di
        pop dx
        pop cx
        pop bx
        mov ax, -1
        stc
        ret

ext2_add_dir_entry:
        ;; Insert a new directory entry in a directory inode.
        ;; Scans direct blocks for a deleted slot or last-entry slack; allocates if needed.
        ;; Input:  AX = dir inode, DI = null-terminated name, BX = new inode number
        ;; Output: CF on error
        push bp
        mov bp, sp
        mov [ext2_ade_inode], bx
        mov [ext2_ade_name], di
        ;; Compute min_rec_len = (8 + namelen + 3) & ~3
        xor cx, cx
        .ead_namelen:
        cmp byte [di], 0
        je .ead_nl_done
        inc cx
        inc di
        jmp .ead_namelen
        .ead_nl_done:
        mov [ext2_ade_namelen], cx
        add cx, 8 + 3
        and cx, 0FFFCh
        mov [ext2_ade_min_rec], cx
        ;; Read dir inode; save 12 direct block pointers
        call ext2_read_inode            ; AX = dir inode → BX = inode ptr
        mov si, bx
        add si, EXT2_INODE_BLOCK
        mov di, ext2_dir_blks
        mov cx, 12
        .ead_save_blks:
        mov ax, [si]
        stosw
        add si, 4
        dec cx
        jnz .ead_save_blks
        ;; Scan each direct block for insertion slot
        mov word [ext2_ade_cur_blk], 0
        .ead_next_blk:
        mov bx, [ext2_ade_cur_blk]
        cmp bx, 12
        jae .ead_alloc_blk
        shl bx, 1
        mov ax, [ext2_dir_blks + bx]
        shr bx, 1
        test ax, ax
        jz .ead_alloc_blk               ; unallocated block → need new block
        xor bx, bx
        call ext2_read_blk_sec          ; AX=block, BX=0 → sector 0; sets ext2_last_blk_sec
        jc .ead_err
        xor bx, bx
        .ead_scan_entry:
        cmp bx, 512                     ; only scan entries that start in sector 0
        jae .ead_next_blk_inc
        mov dx, [SECTOR_BUFFER + bx + EXT2_DIRENT_REC_LEN]
        cmp dx, 8
        jb .ead_err
        mov ax, [SECTOR_BUFFER + bx + EXT2_DIRENT_INODE]
        test ax, ax
        jnz .ead_live_entry
        ;; Deleted entry: use it if large enough
        cmp dx, [ext2_ade_min_rec]
        jb .ead_skip_del
        jmp .ead_insert_here
        .ead_skip_del:
        add bx, dx
        jmp .ead_scan_entry
        .ead_live_entry:
        mov cx, bx
        add cx, dx                      ; CX = offset after this entry
        push cx
        xor ah, ah
        mov al, [ext2_log_block_size]
        mov cl, al
        mov ax, 1024
        shl ax, cl                      ; AX = block_size
        pop cx
        cmp cx, ax                      ; last entry in block?
        jne .ead_live_not_last
        ;; Compute actual_min_rec for this live entry
        xor ch, ch
        mov cl, [SECTOR_BUFFER + bx + EXT2_DIRENT_NAME_LEN]
        add cx, 8 + 3
        and cx, 0FFFCh
        mov ax, dx
        sub ax, cx                      ; AX = slack
        cmp ax, [ext2_ade_min_rec]
        jb .ead_live_not_last
        ;; Split: shorten existing entry; new entry gets the slack
        mov [SECTOR_BUFFER + bx + EXT2_DIRENT_REC_LEN], cx
        add bx, cx
        mov dx, ax                      ; DX = rec_len for new entry
        jmp .ead_insert_here
        .ead_live_not_last:
        add bx, dx
        jmp .ead_scan_entry
        .ead_next_blk_inc:
        inc word [ext2_ade_cur_blk]
        jmp .ead_next_blk
        .ead_alloc_blk:
        ;; Allocate a new data block and store in inode's i_block[cur_blk]
        call ext2_alloc_block           ; AX = new block number, CF on err
        jc .ead_err
        mov [ext2_ade_new_blk], ax
        ;; Re-read dir inode to patch i_block[]
        push ax
        mov cx, [ext2_ade_cur_blk]
        mov ax, [ext2_last_read_inode]
        call ext2_read_inode            ; BX = inode ptr; ext2_last_blk_sec = inode sector
        ;; SI = &i_block[cur_blk]
        push ax                         ; save block_idx result (unused) as temp
        mov ax, cx
        shl ax, 2
        lea si, [bx + EXT2_INODE_BLOCK]
        add si, ax
        pop ax                          ; discard
        pop ax                          ; AX = new block number
        mov [si], ax
        mov word [si+2], 0
        ;; Flush updated inode
        push ax
        mov ax, [ext2_last_blk_sec]
        call write_sector
        pop ax                          ; AX = new block number
        jc .ead_err
        ;; Read sector 0 of new block to set ext2_last_blk_sec
        xor bx, bx
        call ext2_read_blk_sec          ; AX=block, BX=0 → SECTOR_BUFFER; ext2_last_blk_sec set
        jc .ead_err
        ;; Zero SECTOR_BUFFER (directory entries must be zeroed before writing)
        push di
        mov di, SECTOR_BUFFER
        mov cx, 256
        xor ax, ax
        cld
        rep stosw
        pop di
        ;; rec_len for first entry = block_size
        xor ch, ch
        mov cl, [ext2_log_block_size]
        mov dx, 1024
        shl dx, cl                      ; DX = block_size
        xor bx, bx
        .ead_insert_here:
        ;; Write the new directory entry at SECTOR_BUFFER+BX, rec_len=DX
        mov ax, [ext2_ade_inode]
        mov [SECTOR_BUFFER + bx + EXT2_DIRENT_INODE], ax
        mov word [SECTOR_BUFFER + bx + EXT2_DIRENT_INODE + 2], 0
        mov [SECTOR_BUFFER + bx + EXT2_DIRENT_REC_LEN], dx
        mov cx, [ext2_ade_namelen]
        mov [SECTOR_BUFFER + bx + EXT2_DIRENT_NAME_LEN], cl
        mov al, [ext2_ade_filetype]
        mov [SECTOR_BUFFER + bx + EXT2_DIRENT_NAME_LEN + 1], al
        push si
        mov si, [ext2_ade_name]
        lea di, [SECTOR_BUFFER + bx + EXT2_DIRENT_NAME]
        cld
        rep movsb
        pop si
        mov ax, [ext2_last_blk_sec]     ; sector set by read_blk_sec or alloc path
        call write_sector
        pop bp
        ret
        .ead_err:
        pop bp
        stc
        ret

ext2_alloc_bit:
        ;; Find and set first zero bit in a bitmap block.
        ;; Input:  AX = bitmap block number
        ;; Output: AX = allocated bit index (0-based), CF on error
        ;; Side-effect: SECTOR_BUFFER holds modified sector; ext2_last_blk_sec set
        ;; Clobbers: AX, BX, CX, DX, SI
        push di
        mov [ext2_alloc_bitmap_blk], ax
        ;; Scan all sectors of the bitmap block
        xor bx, bx              ; sector 0 first
        .eabit_next_sec:
        mov ax, [ext2_alloc_bitmap_blk]
        call ext2_read_blk_sec  ; AX=block, BX=sector → SECTOR_BUFFER
        jc .eabit_err
        mov si, SECTOR_BUFFER
        mov cx, 512
        .eabit_scan:
        mov al, [si]
        cmp al, 0FFh
        jne .eabit_found_byte
        inc si
        dec cx
        jnz .eabit_scan
        ;; Sector is full; try the next one
        inc bx
        push bx
        xor ch, ch
        mov cl, [ext2_log_block_size]
        inc cl
        mov bx, 1
        shl bx, cl              ; BX = sectors_per_block
        pop cx                  ; CX = next sector index
        cmp cx, bx
        jb .eabit_next_sec_restore
        jmp .eabit_err
        .eabit_next_sec_restore:
        mov bx, cx
        jmp .eabit_next_sec
        .eabit_found_byte:
        ;; AL = byte with a free bit; SI points to it
        not al
        xor dx, dx
        .eabit_bsf:
        test al, 1
        jnz .eabit_got_bit
        shr al, 1
        inc dx
        jmp .eabit_bsf
        .eabit_got_bit:
        ;; DX = bit position within byte (0-7)
        ;; Set the bit in the bitmap
        mov al, 1
        mov cl, dl
        shl al, cl
        or [si], al
        ;; Compute bit index = (sector * 512 + (SI - SECTOR_BUFFER)) * 8 + DX
        mov ax, si
        sub ax, SECTOR_BUFFER           ; AX = byte index within sector
        shl ax, 3                       ; AX = byte_index * 8
        add ax, dx                      ; AX += bit-in-byte
        ;; Add sector offset: sector_num * 512 * 8 = sector_num * 4096
        test bx, bx
        jz .eabit_done
        mov cx, bx
        shl cx, 12              ; CX = bx * 4096
        add ax, cx
        .eabit_done:
        ;; Flush modified bitmap sector
        push ax
        mov ax, [ext2_last_blk_sec]
        call write_sector
        pop ax
        jc .eabit_err2
        pop di
        clc
        ret
        .eabit_err:
        pop di
        stc
        ret
        .eabit_err2:
        pop di
        stc
        ret

ext2_alloc_block:
        ;; Allocate one block from the block bitmap.
        ;; Output: AX = block number, CF on err
        ;; Block number = bit_index + first_data_block (for 1KB blocks, first_data_block=1)
        push bx
        mov ax, [ext2_block_bitmap_blk]
        call ext2_alloc_bit     ; AX = bit index, CF on err
        jc .eab_err
        add ax, [ext2_first_data_block]    ; block number = bit_index + first_data_block
        push ax
        call ext2_bgd_block_alloc
        pop ax
        pop bx
        clc
        ret
        .eab_err:
        pop bx
        stc
        ret

ext2_alloc_inode:
        ;; Allocate one inode from the inode bitmap.
        ;; Output: AX = inode number (1-based), CF on err
        push bx
        mov ax, [ext2_inode_bitmap_blk]
        call ext2_alloc_bit     ; AX = bit index
        jc .eai_err
        inc ax                  ; inodes are 1-based
        push ax
        call ext2_bgd_inode_alloc
        pop ax
        pop bx
        clc
        ret
        .eai_err:
        pop bx
        stc
        ret

ext2_bgd_block_alloc:
        ;; Decrement bg_free_blocks_count in BGD and s_free_blocks_count in superblock.
        ;; Clobbers: AX, BX, CX, DX
        xor bx, bx
        mov ax, [ext2_bgd_block]
        call ext2_read_blk_sec
        dec word [SECTOR_BUFFER + EXT2_BGD_FREE_BLOCKS_COUNT]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        mov ax, EXT2_START_SECTOR + 2
        call read_sector
        dec word [SECTOR_BUFFER + EXT2_SB_FREE_BLOCKS_COUNT]
        mov ax, EXT2_START_SECTOR + 2
        call write_sector
        ret

ext2_bgd_block_free:
        ;; Increment bg_free_blocks_count in BGD and s_free_blocks_count in superblock.
        ;; Clobbers: AX, BX, CX, DX
        xor bx, bx
        mov ax, [ext2_bgd_block]
        call ext2_read_blk_sec
        inc word [SECTOR_BUFFER + EXT2_BGD_FREE_BLOCKS_COUNT]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        mov ax, EXT2_START_SECTOR + 2
        call read_sector
        inc word [SECTOR_BUFFER + EXT2_SB_FREE_BLOCKS_COUNT]
        mov ax, EXT2_START_SECTOR + 2
        call write_sector
        ret

ext2_bgd_dir_alloc:
        ;; Increment bg_used_dirs_count in BGD.
        ;; Clobbers: AX, BX, CX, DX
        xor bx, bx
        mov ax, [ext2_bgd_block]
        call ext2_read_blk_sec
        inc word [SECTOR_BUFFER + EXT2_BGD_USED_DIRS_COUNT]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        ret

ext2_bgd_dir_free:
        ;; Decrement bg_used_dirs_count in BGD.
        ;; Clobbers: AX, BX, CX, DX
        xor bx, bx
        mov ax, [ext2_bgd_block]
        call ext2_read_blk_sec
        dec word [SECTOR_BUFFER + EXT2_BGD_USED_DIRS_COUNT]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        ret

ext2_bgd_inode_alloc:
        ;; Decrement bg_free_inodes_count in BGD and s_free_inodes_count in superblock.
        ;; Clobbers: AX, BX, CX, DX
        xor bx, bx
        mov ax, [ext2_bgd_block]
        call ext2_read_blk_sec
        dec word [SECTOR_BUFFER + EXT2_BGD_FREE_INODES_COUNT]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        mov ax, EXT2_START_SECTOR + 2
        call read_sector
        dec word [SECTOR_BUFFER + EXT2_SB_FREE_INODES_COUNT]
        mov ax, EXT2_START_SECTOR + 2
        call write_sector
        ret

ext2_bgd_inode_free:
        ;; Increment bg_free_inodes_count in BGD and s_free_inodes_count in superblock.
        ;; Clobbers: AX, BX, CX, DX
        xor bx, bx
        mov ax, [ext2_bgd_block]
        call ext2_read_blk_sec
        inc word [SECTOR_BUFFER + EXT2_BGD_FREE_INODES_COUNT]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        mov ax, EXT2_START_SECTOR + 2
        call read_sector
        inc word [SECTOR_BUFFER + EXT2_SB_FREE_INODES_COUNT]
        mov ax, EXT2_START_SECTOR + 2
        call write_sector
        ret

ext2_chmod:
        ;; Set or clear execute permission on a file.
        ;; Input:  SI = path, AL = mode (FLAG_EXECUTE sets +x; 0 clears -x)
        ;; Output: CF on error, AL = error code
        push bx
        push cx
        push dx
        push si
        push di
        push ax                         ; save mode byte
        call ext2_find                  ; SI=path → vfs_found_inode; CF if not found
        jc .echm_not_found
        mov ax, [vfs_found_inode]
        call ext2_read_inode            ; BX = inode ptr in SECTOR_BUFFER
        pop ax                          ; AL = mode flags
        test al, FLAG_EXECUTE
        jz .echm_clear
        or word [bx + EXT2_INODE_MODE], EXT2_S_IXALL
        jmp .echm_flush
        .echm_clear:
        and word [bx + EXT2_INODE_MODE], ~EXT2_S_IXALL
        .echm_flush:
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .echm_err
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .echm_not_found:
        pop ax                          ; discard mode byte
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .echm_err:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_commit_write_sec:
        ;; Write SECTOR_BUFFER to the last sector read by ext2_prepare_write_sec.
        ;; Output: CF on disk error
        mov ax, [ext2_last_blk_sec]
        call write_sector
        ret

ext2_create:
        ;; Create a new regular file.
        ;; Input:  SI = null-terminated path (e.g. "file" or "dir/file"), DL = mode flags
        ;; Output: vfs_found_* set, CF on error
        ;; Clobbers: AX, BX, CX, DX, SI, DI
        push bp
        mov bp, sp
        mov [ext2_cr_name], si
        mov [ext2_cr_mode], dl
        ;; Resolve parent directory and basename from path
        call ext2_resolve_path          ; AX = parent inode, SI = basename; CF if not found
        jc .ecr_err
        mov [ext2_cr_parent_inode], ax
        mov [ext2_cr_name], si          ; narrow to basename only
        ;; Allocate a new inode
        call ext2_alloc_inode   ; AX = inode number, CF on err
        jc .ecr_err
        mov [ext2_cr_new_inode], ax
        ;; Initialise the inode sector (ext2_alloc_inode left bitmap sector in SECTOR_BUFFER)
        ;; Read the inode sector fresh
        call ext2_read_inode    ; AX = new inode → BX = pointer in SECTOR_BUFFER
        ;; Zero the inode (128 or 256 bytes)
        push si
        push cx
        mov si, bx
        mov cx, [ext2_inode_size]
        xor ax, ax
        cld
        .ecr_zero_inode:
        mov [si], ax
        add si, 2
        sub cx, 2
        jnz .ecr_zero_inode
        pop cx
        pop si
        ;; Set i_mode: EXT2_S_IFREG | 0644
        mov ax, EXT2_S_IFREG | 0644o
        ;; Apply execute flag
        mov dl, [ext2_cr_mode]
        test dl, FLAG_EXECUTE
        jz .ecr_no_exec
        or ax, EXT2_S_IXALL
        .ecr_no_exec:
        mov [bx + EXT2_INODE_MODE], ax
        ;; links_count = 1
        mov word [bx + EXT2_INODE_LINKS_COUNT], 1
        ;; Flush inode sector
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .ecr_err
        ;; Add directory entry in parent directory
        mov byte [ext2_ade_filetype], 1    ; FT_REG_FILE
        mov ax, [ext2_cr_parent_inode]
        mov di, [ext2_cr_name]
        mov bx, [ext2_cr_new_inode]
        call ext2_add_dir_entry
        jc .ecr_err
        ;; Populate vfs_found_*
        mov ax, [ext2_cr_new_inode]
        mov [vfs_found_inode], ax
        mov dword [vfs_found_size], 0
        mov byte [vfs_found_type], FD_TYPE_FILE
        mov al, [ext2_cr_mode]
        mov [vfs_found_mode], al
        mov word [vfs_found_dir_sec], 0     ; not used for ext2
        mov word [vfs_found_dir_off], 0
        pop bp
        clc
        ret
        .ecr_err:
        pop bp
        stc
        ret

ext2_delete:
        ;; Delete a regular file: free its data blocks and inode, remove dir entry.
        ;; Directories are rejected.
        ;; Input:  SI = path
        ;; Output: CF clear on success; CF set, AL = error code on failure
        push bx
        push cx
        push dx
        push si
        push di
        call ext2_resolve_path          ; AX=parent_inode, SI=basename; CF if not found
        jc .edl_not_found
        mov [ext2_dl_parent_inode], ax
        mov [ext2_dl_name], si
        call ext2_search_dir            ; AX=file_inode; CF if not found
        jc .edl_not_found
        mov [ext2_dl_inode], ax
        call ext2_read_inode            ; BX = inode ptr in SECTOR_BUFFER
        test word [bx + EXT2_INODE_MODE], EXT2_S_IFDIR
        jnz .edl_not_found
        ;; Save block pointers (direct 0-11, indirect 12, doubly-indirect 13)
        push si
        lea si, [bx + EXT2_INODE_BLOCK]
        mov di, ext2_dl_blks
        mov cx, 14
        cld
        .edl_save_blks:
        mov ax, [si]
        stosw
        add si, 4
        dec cx
        jnz .edl_save_blks
        pop si
        ;; Set i_dtime, zero i_links_count, flush inode before freeing blocks
        call rtc_read_epoch             ; DX:AX = epoch; BX and SECTOR_BUFFER preserved
        mov [bx + EXT2_INODE_DTIME], ax
        mov [bx + EXT2_INODE_DTIME + 2], dx
        mov word [bx + EXT2_INODE_LINKS_COUNT], 0
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .edl_err
        ;; Free direct blocks 0-11
        mov bx, ext2_dl_blks
        mov cx, 12
        .edl_free_direct:
        mov ax, [bx]
        test ax, ax
        jz .edl_next_direct
        push bx
        push cx
        call ext2_free_block
        pop cx
        pop bx
        jc .edl_err
        .edl_next_direct:
        add bx, 2
        dec cx
        jnz .edl_free_direct
        ;; Free singly-indirect block i_block[12] and all data blocks it points to
        mov ax, [ext2_dl_blks + 24]    ; i_block[12]: 12×2 = offset 24
        call ext2_free_ind_block        ; AX=0 is a no-op; CF on error
        jc .edl_err
        ;; Free doubly-indirect block i_block[13]
        mov ax, [ext2_dl_blks + 26]    ; i_block[13]: 13×2 = offset 26
        test ax, ax
        jz .edl_free_inode
        mov [ext2_dl_dbl_blk], ax
        xor cx, cx
        mov cl, [ext2_log_block_size]
        mov ax, 256
        shl ax, cl
        mov [ext2_dl_dbl_count], ax
        mov word [ext2_dl_dbl_idx], 0
        .edl_dbl_loop:
        mov ax, [ext2_dl_dbl_idx]
        cmp ax, [ext2_dl_dbl_count]
        jae .edl_dbl_free_self
        mov bx, ax
        shr bx, 7
        mov ax, [ext2_dl_dbl_blk]
        call ext2_read_blk_sec          ; re-read each iteration (clobbered by free_ind_block)
        jc .edl_err
        mov bx, [ext2_dl_dbl_idx]
        and bx, 07Fh
        shl bx, 2
        mov ax, [SECTOR_BUFFER + bx]
        test ax, ax
        jz .edl_dbl_next
        call ext2_free_ind_block
        jc .edl_err
        .edl_dbl_next:
        inc word [ext2_dl_dbl_idx]
        jmp .edl_dbl_loop
        .edl_dbl_free_self:
        mov ax, [ext2_dl_dbl_blk]
        call ext2_free_block
        jc .edl_err
        .edl_free_inode:
        mov ax, [ext2_dl_inode]
        call ext2_free_inode
        jc .edl_err
        mov ax, [ext2_dl_parent_inode]
        mov si, [ext2_dl_name]
        call ext2_remove_dir_entry
        jc .edl_err
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .edl_not_found:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .edl_err:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_free_bit:
        ;; Clear a bit in a bitmap block (inverse of ext2_alloc_bit).
        ;; Input:  AX = bitmap block number, BX = bit index (0-based)
        ;; Output: CF on disk error
        ;; Clobbers: AX, BX, CX, SI
        push di
        mov [ext2_fb_bitmap_blk], ax
        mov cx, bx
        shr cx, 12                      ; CX = sector within block (bit_idx / 4096)
        push bx
        mov bx, cx
        call ext2_read_blk_sec          ; AX=block, BX=sector → SECTOR_BUFFER
        pop bx
        jc .efb_err
        ;; Byte offset within sector = (bit_idx & 0xFFF) >> 3
        mov si, bx
        and si, 0FFFh
        shr si, 3
        ;; Build clear mask: ~(1 << (bit_idx & 7))
        mov cl, bl
        and cl, 7
        mov al, 1
        shl al, cl
        not al
        and [SECTOR_BUFFER + si], al
        mov ax, [ext2_last_blk_sec]
        call write_sector
        pop di
        ret
        .efb_err:
        pop di
        stc
        ret

ext2_free_block:
        ;; Free one block from the block bitmap.
        ;; Input:  AX = block number
        ;; Output: CF on disk error
        push bx
        sub ax, [ext2_first_data_block]    ; bit_index = block_number - first_data_block
        mov bx, ax
        mov ax, [ext2_block_bitmap_blk]
        call ext2_free_bit
        jc .efbl_err
        call ext2_bgd_block_free
        pop bx
        clc
        ret
        .efbl_err:
        pop bx
        stc
        ret

ext2_free_inode:
        ;; Free one inode from the inode bitmap.
        ;; Input:  AX = inode number (1-based)
        ;; Output: CF on disk error
        push bx
        dec ax                          ; convert 1-based to 0-based bit index
        mov bx, ax
        mov ax, [ext2_inode_bitmap_blk]
        call ext2_free_bit
        jc .efi_err
        call ext2_bgd_inode_free
        pop bx
        clc
        ret
        .efi_err:
        pop bx
        stc
        ret

ext2_prepare_write_sec:
        ;; Prepare for a write: find or allocate the block for fd's current
        ;; position, read that sector into SECTOR_BUFFER, return byte offset.
        ;; Input:  SI = fd_entry pointer (FD_OFFSET_START=inode, FD_OFFSET_POSITION=pos)
        ;; Output: SECTOR_BUFFER ready for modification, BX = byte offset; CF on err
        ;; Side-effect: ext2_last_blk_sec set for ext2_commit_write_sec
        push ax
        push cx
        push dx
        ;; Decompose 32-bit position (matching ext2_read_sec)
        mov ax, [si+FD_OFFSET_POSITION]
        mov dx, [si+FD_OFFSET_POSITION+2]
        mov bx, ax
        and bx, 01FFh                   ; BX = byte offset within sector
        mov [ext2_pws_byte_offset], bx
        push bx                         ; save for return
        shr ax, 9
        shl dx, 7
        or ax, dx                       ; AX = sector_index
        xor ch, ch
        mov cl, [ext2_log_block_size]
        inc cl                          ; cl = log+1
        mov dx, ax                      ; DX = sector_index
        shr ax, cl                      ; AX = block_index
        mov [ext2_pws_block_idx], ax
        mov ax, 1
        shl ax, cl
        dec ax
        and dx, ax                      ; DX = sector_in_block
        mov [ext2_pws_sec_in_blk], dx
        ;; Read inode; try to resolve existing block
        mov ax, [si+FD_OFFSET_START]
        call ext2_read_inode            ; BX = inode ptr
        mov ax, [ext2_pws_block_idx]
        call ext2_get_data_block        ; AX=block_idx, BX=inode_ptr → AX=block_num
        jc .epws_alloc
        test ax, ax
        jz .epws_alloc
        jmp .epws_have_block
        .epws_alloc:
        mov ax, [ext2_pws_block_idx]
        cmp ax, 12
        jb .epws_alloc_direct
        ;; ---- Singly-indirect path ----
        ;; Re-read inode; check if i_block[12] (indirect block) already exists
        mov ax, [ext2_last_read_inode]
        call ext2_read_inode            ; BX = inode ptr
        mov ax, [bx + EXT2_INODE_BLOCK + 48]   ; low word of i_block[12]
        test ax, ax
        jnz .epws_have_ind_blk
        ;; Allocate and zero-fill a new indirect block
        call ext2_alloc_block           ; AX = ind_blk; SI clobbered; SECTOR_BUFFER clobbered
        jc .epws_err
        mov [ext2_pws_ind_blk], ax
        push di
        mov di, SECTOR_BUFFER
        mov cx, 256
        xor ax, ax
        cld
        rep stosw
        pop di
        ;; Write zeros to each sector of the new indirect block
        xor bx, bx                      ; BX = sector counter
        .epws_zero_ind:
        movzx cx, byte [ext2_log_block_size]
        inc cx                          ; CL = log+1
        mov ax, 1
        shl ax, cl                      ; AX = sectors_per_block
        cmp bx, ax
        jae .epws_ind_zeroed
        push bx
        mov ax, [ext2_pws_ind_blk]
        shl ax, cl                      ; AX = first relative sector of ind_blk
        add ax, EXT2_START_SECTOR
        add ax, bx
        call write_sector
        pop bx
        jc .epws_err
        inc bx
        jmp .epws_zero_ind
        .epws_ind_zeroed:
        ;; Re-read inode; store ind_blk in i_block[12]; update i_blocks; flush
        mov ax, [ext2_last_read_inode]
        call ext2_read_inode            ; BX = inode ptr
        mov ax, [ext2_pws_ind_blk]
        mov [bx + EXT2_INODE_BLOCK + 48], ax
        mov word [bx + EXT2_INODE_BLOCK + 50], 0
        movzx cx, byte [ext2_log_block_size]
        inc cx
        push ax                         ; save ind_blk
        mov ax, 1
        shl ax, cl
        add [bx + EXT2_INODE_BLOCKS], ax
        adc word [bx + EXT2_INODE_BLOCKS + 2], 0
        mov ax, [ext2_last_blk_sec]
        call write_sector
        pop ax                          ; AX = ind_blk (restore)
        jc .epws_err
        .epws_have_ind_blk:
        ;; AX = indirect block number
        mov [ext2_pws_ind_blk], ax
        mov ax, [ext2_pws_block_idx]
        sub ax, 12
        mov [ext2_pws_ptr_idx], ax      ; ptr_idx = block_idx - 12
        ;; Allocate data block
        call ext2_alloc_block           ; AX = data_blk; SI clobbered; SECTOR_BUFFER clobbered
        jc .epws_err
        push ax                         ; save data_blk
        ;; Write data_blk into the indirect block at ptr_idx
        mov bx, [ext2_pws_ptr_idx]
        shr bx, 7                       ; BX = sector within indirect block
        mov ax, [ext2_pws_ind_blk]
        call ext2_read_blk_sec
        pop ax                          ; AX = data_blk (restore)
        push ax                         ; re-save
        jc .epws_err_dblk
        mov bx, [ext2_pws_ptr_idx]
        and bx, 07Fh
        shl bx, 2                       ; BX = byte offset within sector
        mov [SECTOR_BUFFER + bx], ax
        mov word [SECTOR_BUFFER + bx + 2], 0
        call ext2_write_blk_sec
        pop ax                          ; AX = data_blk (restore for have_block)
        jc .epws_err
        ;; Update i_blocks in inode for the newly allocated data block
        push ax                         ; save data_blk
        mov ax, [ext2_last_read_inode]
        call ext2_read_inode            ; BX = inode ptr
        movzx cx, byte [ext2_log_block_size]
        inc cx
        push bx                         ; save inode ptr
        mov ax, 1
        shl ax, cl
        pop bx
        add [bx + EXT2_INODE_BLOCKS], ax
        adc word [bx + EXT2_INODE_BLOCKS + 2], 0
        mov ax, [ext2_last_blk_sec]
        call write_sector
        pop ax                          ; AX = data_blk (restore)
        jc .epws_err
        jmp .epws_have_block
        ;; ---- Direct block path ----
        .epws_alloc_direct:
        call ext2_alloc_block           ; AX = new block; SI clobbered; SECTOR_BUFFER clobbered
        jc .epws_err
        push ax                         ; save block number
        mov ax, [ext2_last_read_inode]
        call ext2_read_inode            ; BX = inode ptr
        ;; Update i_blocks
        movzx cx, byte [ext2_log_block_size]
        inc cx
        mov ax, 1
        shl ax, cl
        add [bx + EXT2_INODE_BLOCKS], ax
        adc word [bx + EXT2_INODE_BLOCKS + 2], 0
        ;; Store block pointer in i_block[block_idx]
        mov ax, [ext2_pws_block_idx]
        shl ax, 2
        add bx, EXT2_INODE_BLOCK
        add bx, ax                      ; BX → i_block[block_idx]
        pop ax                          ; AX = new block number
        mov [bx], ax
        mov word [bx+2], 0
        push ax                         ; re-save block number for have_block
        mov ax, [ext2_last_blk_sec]
        call write_sector
        pop ax
        jc .epws_err
        .epws_have_block:
        ;; AX = block number; read the sector (or skip if byte_offset=0)
        cmp word [ext2_pws_byte_offset], 0
        jne .epws_do_read
        ;; Skip read: just set ext2_last_blk_sec for write-back
        push cx
        movzx cx, byte [ext2_log_block_size]
        inc cx
        shl ax, cl
        add ax, EXT2_START_SECTOR
        add ax, [ext2_pws_sec_in_blk]
        mov [ext2_last_blk_sec], ax
        pop cx
        jmp .epws_read_done
        .epws_do_read:
        mov bx, [ext2_pws_sec_in_blk]
        call ext2_read_blk_sec          ; AX=block, BX=sector_in_block → SECTOR_BUFFER
        jc .epws_err
        .epws_read_done:
        pop bx                          ; BX = byte offset within sector
        pop dx
        pop cx
        pop ax
        clc
        ret
        .epws_err_dblk:
        pop ax                          ; discard saved data_blk
        .epws_err:
        add sp, 2                       ; discard saved byte offset
        pop dx
        pop cx
        pop ax
        stc
        ret

ext2_remove_dir_entry:
        ;; Delete an entry by name from a directory (sets its inode field to 0).
        ;; Input:  AX = dir_inode, SI = name
        ;; Output: CF on error (not found or disk error)
        push bx
        push cx
        push dx
        push di
        mov [ext2_rde_name], si
        ;; Read dir inode; save direct block pointers
        call ext2_read_inode            ; BX = inode ptr in SECTOR_BUFFER
        mov si, bx
        add si, EXT2_INODE_BLOCK
        mov di, ext2_dir_blks
        mov cx, 12
        .erde_save:
        mov ax, [si]
        stosw
        add si, 4
        dec cx
        jnz .erde_save
        ;; Scan sector 0 of each direct block
        xor cx, cx
        .erde_next_blk:
        cmp cx, 12
        jae .erde_not_found
        mov bx, cx
        shl bx, 1
        mov ax, [ext2_dir_blks + bx]
        test ax, ax
        jz .erde_not_found
        push cx
        xor bx, bx
        call ext2_read_blk_sec          ; AX=block, BX=0 → SECTOR_BUFFER
        pop cx
        jc .erde_err
        xor bx, bx
        .erde_scan:
        cmp bx, 512
        jae .erde_blk_done
        mov dx, [SECTOR_BUFFER + bx + EXT2_DIRENT_REC_LEN]
        cmp dx, 8
        jb .erde_err
        mov ax, [SECTOR_BUFFER + bx + EXT2_DIRENT_INODE]
        test ax, ax
        jz .erde_advance
        push bx
        push cx
        push dx
        lea di, [SECTOR_BUFFER + bx + EXT2_DIRENT_NAME]
        mov si, [ext2_rde_name]
        mov cl, [SECTOR_BUFFER + bx + EXT2_DIRENT_NAME_LEN]
        call ext2_names_match           ; CF = no match
        pop dx
        pop cx
        pop bx
        jc .erde_advance
        ;; Found: zero the inode field and flush the sector
        mov word [SECTOR_BUFFER + bx + EXT2_DIRENT_INODE], 0
        mov word [SECTOR_BUFFER + bx + EXT2_DIRENT_INODE + 2], 0
        mov ax, [ext2_last_blk_sec]
        call write_sector               ; CF on disk error
        pop di
        pop dx
        pop cx
        pop bx
        ret
        .erde_advance:
        add bx, dx
        jmp .erde_scan
        .erde_blk_done:
        inc cx
        jmp .erde_next_blk
        .erde_not_found:
        pop di
        pop dx
        pop cx
        pop bx
        stc
        ret
        .erde_err:
        pop di
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_rename:
        ;; Rename or move a file or directory.
        ;; Input:  SI = old path, DI = new path
        ;; Output: CF on error, AL = error code
        push bx
        push cx
        push dx
        push si
        push di
        mov [ext2_rn_old_path], si
        mov [ext2_rn_new_path], di
        ;; Reject if new name already exists
        mov si, di
        call ext2_find
        jc .ern_dest_ok
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        mov al, ERROR_EXISTS
        stc
        ret
        .ern_dest_ok:
        ;; Resolve old path → (old_dir_inode, old_basename)
        mov si, [ext2_rn_old_path]
        call ext2_resolve_path          ; AX = dir inode, SI = basename; CF if not found
        jc .ern_not_found
        mov [ext2_rn_old_dir], ax
        mov [ext2_rn_old_name], si
        ;; Look up old entry's inode
        call ext2_search_dir            ; AX = dir inode, SI = basename → AX = inode; CF if not found
        jc .ern_not_found
        mov [ext2_rn_old_inode], ax
        ;; Resolve new path → (new_dir_inode, new_basename)
        mov si, [ext2_rn_new_path]
        call ext2_resolve_path          ; AX = dir inode, SI = basename; CF if parent not found
        jc .ern_not_found
        mov [ext2_rn_new_dir], ax
        mov [ext2_rn_new_name], si
        ;; Determine filetype from old inode's mode
        mov ax, [ext2_rn_old_inode]
        call ext2_read_inode            ; BX = old inode ptr; clobbers AX, CX, DX
        mov byte [ext2_ade_filetype], 1
        test word [bx + EXT2_INODE_MODE], EXT2_S_IFDIR
        jz .ern_filetype_set
        mov byte [ext2_ade_filetype], 2
        .ern_filetype_set:
        ;; Add new directory entry
        mov ax, [ext2_rn_new_dir]
        mov di, [ext2_rn_new_name]
        mov bx, [ext2_rn_old_inode]
        call ext2_add_dir_entry
        jc .ern_err
        ;; Remove old directory entry
        mov ax, [ext2_rn_old_dir]
        mov si, [ext2_rn_old_name]
        call ext2_remove_dir_entry
        jc .ern_err
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .ern_not_found:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .ern_err:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_check_dir_empty:
        ;; Scan all sectors of ext2 directory block AX for entries other than '.' and '..'
        ;; Output: CF clear if only . / .. found; CF set if any other live entry exists
        push bx
        push cx
        push dx
        push si
        push di
        mov [ext2_rdr_cde_blk], ax
        xor cx, cx
        mov cl, [ext2_log_block_size]
        inc cl
        mov dx, 1
        shl dx, cl                      ; DX = sectors_per_block
        xor bx, bx                      ; BX = sector index
        .ecde_next_sec:
        cmp bx, dx
        jae .ecde_empty
        push dx
        mov ax, [ext2_rdr_cde_blk]
        call ext2_read_blk_sec          ; AX=block, BX=sector → SECTOR_BUFFER; BX unchanged
        pop dx
        jc .ecde_err
        mov si, SECTOR_BUFFER
        .ecde_entry:
        mov di, si
        sub di, SECTOR_BUFFER
        cmp di, 512
        jae .ecde_next_sec2
        mov cx, [si + EXT2_DIRENT_REC_LEN]
        cmp cx, EXT2_DIRENT_NAME        ; < 8 is invalid
        jb .ecde_next_sec2
        mov ax, [si + EXT2_DIRENT_INODE]
        test ax, ax
        jz .ecde_advance                ; deleted entry: skip
        xor ah, ah
        mov al, [si + EXT2_DIRENT_NAME_LEN]
        cmp al, 1
        jne .ecde_check_dotdot
        cmp byte [si + EXT2_DIRENT_NAME], '.'
        je .ecde_advance                ; "." entry: skip
        jmp .ecde_not_empty
        .ecde_check_dotdot:
        cmp al, 2
        jne .ecde_not_empty
        cmp byte [si + EXT2_DIRENT_NAME], '.'
        jne .ecde_not_empty
        cmp byte [si + EXT2_DIRENT_NAME + 1], '.'
        jne .ecde_not_empty
        .ecde_advance:
        add si, cx
        jmp .ecde_entry
        .ecde_next_sec2:
        inc bx
        jmp .ecde_next_sec
        .ecde_not_empty:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret
        .ecde_empty:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .ecde_err:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_rmdir:
        ;; Remove a directory that contains only '.' and '..'.
        ;; Frees data blocks, frees inode, removes dir entry from parent,
        ;; and decrements parent directory's i_links_count.
        ;; Input:  SI = path
        ;; Output: CF clear on success; CF set, AL = error code on failure
        push bx
        push cx
        push dx
        push si
        push di
        call ext2_resolve_path          ; AX=parent_inode, SI=basename; CF if not found
        jc .erdr_not_found
        mov [ext2_rdr_parent_inode], ax
        mov [ext2_rdr_name], si
        call ext2_search_dir            ; AX=dir_inode; CF if not found
        jc .erdr_not_found
        mov [ext2_rdr_inode], ax
        call ext2_read_inode            ; BX = inode ptr in SECTOR_BUFFER
        test word [bx + EXT2_INODE_MODE], EXT2_S_IFDIR
        jz .erdr_not_found
        ;; Save block pointers (direct 0-11, indirect 12, doubly-indirect 13)
        push si
        lea si, [bx + EXT2_INODE_BLOCK]
        mov di, ext2_rdr_blks
        mov cx, 14
        cld
        .erdr_save_blks:
        mov ax, [si]
        stosw
        add si, 4
        dec cx
        jnz .erdr_save_blks
        pop si
        ;; Check each direct block for non-./.. entries (indirect blocks unsupported for dirs)
        mov bx, ext2_rdr_blks
        .erdr_check_blk:
        mov ax, [bx]
        test ax, ax
        jz .erdr_checked
        push bx
        call ext2_check_dir_empty       ; AX=block; CF if non-empty entry found
        pop bx
        jc .erdr_not_empty
        add bx, 2
        cmp bx, ext2_rdr_blks + 24     ; past 12 direct blocks?
        jb .erdr_check_blk
        .erdr_checked:
        ;; Directory is empty: re-read inode (check_dir_empty clobbers SECTOR_BUFFER)
        mov ax, [ext2_rdr_inode]
        call ext2_read_inode            ; BX = inode ptr
        ;; Set i_dtime, zero i_links_count, flush before freeing blocks
        call rtc_read_epoch             ; DX:AX = epoch; BX and SECTOR_BUFFER preserved
        mov [bx + EXT2_INODE_DTIME], ax
        mov [bx + EXT2_INODE_DTIME + 2], dx
        mov word [bx + EXT2_INODE_LINKS_COUNT], 0
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .erdr_err
        ;; Free direct blocks 0-11
        mov bx, ext2_rdr_blks
        mov cx, 12
        .erdr_free_direct:
        mov ax, [bx]
        test ax, ax
        jz .erdr_next_direct
        push bx
        push cx
        call ext2_free_block
        pop cx
        pop bx
        jc .erdr_err
        .erdr_next_direct:
        add bx, 2
        dec cx
        jnz .erdr_free_direct
        ;; Free singly-indirect block i_block[12]
        mov ax, [ext2_rdr_blks + 24]
        call ext2_free_ind_block
        jc .erdr_err
        ;; Free doubly-indirect block i_block[13]
        mov ax, [ext2_rdr_blks + 26]
        test ax, ax
        jz .erdr_free_inode
        mov [ext2_rdr_dbl_blk], ax
        xor cx, cx
        mov cl, [ext2_log_block_size]
        mov ax, 256
        shl ax, cl
        mov [ext2_rdr_dbl_count], ax
        mov word [ext2_rdr_dbl_idx], 0
        .erdr_dbl_loop:
        mov ax, [ext2_rdr_dbl_idx]
        cmp ax, [ext2_rdr_dbl_count]
        jae .erdr_dbl_free_self
        mov bx, ax
        shr bx, 7
        mov ax, [ext2_rdr_dbl_blk]
        call ext2_read_blk_sec
        jc .erdr_err
        mov bx, [ext2_rdr_dbl_idx]
        and bx, 07Fh
        shl bx, 2
        mov ax, [SECTOR_BUFFER + bx]
        test ax, ax
        jz .erdr_dbl_next
        call ext2_free_ind_block
        jc .erdr_err
        .erdr_dbl_next:
        inc word [ext2_rdr_dbl_idx]
        jmp .erdr_dbl_loop
        .erdr_dbl_free_self:
        mov ax, [ext2_rdr_dbl_blk]
        call ext2_free_block
        jc .erdr_err
        .erdr_free_inode:
        mov ax, [ext2_rdr_inode]
        call ext2_free_inode
        jc .erdr_err
        mov ax, [ext2_rdr_parent_inode]
        mov si, [ext2_rdr_name]
        call ext2_remove_dir_entry
        jc .erdr_err
        ;; Decrement parent directory's i_links_count
        mov ax, [ext2_rdr_parent_inode]
        call ext2_read_inode            ; BX = inode ptr
        dec word [bx + EXT2_INODE_LINKS_COUNT]
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .erdr_err
        call ext2_bgd_dir_free
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        clc
        ret
        .erdr_not_found:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        mov al, ERROR_NOT_FOUND
        stc
        ret
        .erdr_not_empty:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        mov al, ERROR_NOT_EMPTY
        stc
        ret
        .erdr_err:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        stc
        ret

ext2_update_size:
        ;; Write fd position back to inode i_size (32-bit).
        ;; Grows i_size if position > current size; on shrink, frees orphaned blocks.
        ;; Input:  SI = fd_entry pointer
        ;; Output: CF on disk error
        push ax
        push bx
        push cx
        push dx
        push si
        push di
        mov [ext2_us_fd], si
        mov ax, [si+FD_OFFSET_START]
        call ext2_read_inode                ; BX = inode ptr in SECTOR_BUFFER
        ;; 32-bit compare: new_pos vs old i_size
        movzx eax, word [si+FD_OFFSET_POSITION]
        movzx edx, word [si+FD_OFFSET_POSITION+2]
        shl edx, 16
        or eax, edx                         ; EAX = new_pos
        movzx ecx, word [bx + EXT2_INODE_SIZE_LO]
        movzx edx, word [bx + EXT2_INODE_SIZE_LO + 2]
        shl edx, 16
        or ecx, edx                         ; ECX = old_size
        cmp eax, ecx
        ja .eus_grow
        je .eus_no_update
        ;; Shrink: compute keep_blocks = ceil(new_pos / block_size)
        movzx ecx, byte [ext2_log_block_size]
        add cl, 10                          ; CL = block_size_shift (10 for 1 KB blocks)
        mov edx, 1
        shl edx, cl
        dec edx                             ; EDX = block_size - 1
        add eax, edx
        shr eax, cl                         ; EAX = keep_blocks
        mov [ext2_us_keep_blocks], ax
        ;; Save i_block[0..13] (low 16 bits of each 4-byte entry)
        push si
        lea si, [bx + EXT2_INODE_BLOCK]
        mov di, ext2_us_blks
        mov cx, 14
        cld
        .eus_save_blks:
        mov ax, [si]
        stosw
        add si, 4
        dec cx
        jnz .eus_save_blks
        pop si
        ;; Update i_size and zero freed i_block[] entries in SECTOR_BUFFER
        mov ax, [si+FD_OFFSET_POSITION]
        mov [bx + EXT2_INODE_SIZE_LO], ax
        mov ax, [si+FD_OFFSET_POSITION+2]
        mov [bx + EXT2_INODE_SIZE_LO + 2], ax
        mov cx, [ext2_us_keep_blocks]
        cmp cx, 14
        jae .eus_flush
        mov di, cx
        shl di, 2
        add di, EXT2_INODE_BLOCK
        add di, bx                          ; DI → i_block[keep_blocks] in SECTOR_BUFFER
        mov cx, 14
        sub cx, [ext2_us_keep_blocks]
        shl cx, 1                           ; words to zero (each entry = 4 bytes = 2 words)
        xor ax, ax
        rep stosw
        .eus_flush:
        ;; Update i_blocks = total_blocks_kept * sectors_per_block
        mov ax, [ext2_us_keep_blocks]
        cmp ax, 12
        jbe .eus_ib_no_ind
        inc ax                          ; +1 for singly-indirect pointer block
        .eus_ib_no_ind:
        xor cx, cx
        mov cl, [ext2_log_block_size]
        inc cl                          ; sectors_per_block shift = log_block_size + 1
        shl ax, cl
        mov [bx + EXT2_INODE_BLOCKS], ax
        mov word [bx + EXT2_INODE_BLOCKS + 2], 0
        ;; Flush inode to disk before freeing blocks
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .eus_err
        ;; Free direct blocks [keep_blocks..11]
        mov cx, [ext2_us_keep_blocks]
        .eus_free_direct_loop:
        cmp cx, 12
        jae .eus_indirect
        mov di, cx
        shl di, 1
        mov ax, [ext2_us_blks + di]
        test ax, ax
        jz .eus_next_direct
        push cx
        call ext2_free_block
        pop cx
        jc .eus_err
        .eus_next_direct:
        inc cx
        jmp .eus_free_direct_loop
        .eus_indirect:
        ;; Handle singly-indirect block i_block[12]
        mov ax, [ext2_us_blks + 24]
        test ax, ax
        jz .eus_doubly
        mov [ext2_us_ind_blk], ax
        ;; ind_start = max(0, keep_blocks - 12)
        mov ax, [ext2_us_keep_blocks]
        cmp ax, 12
        jbe .eus_ind_start_zero
        sub ax, 12
        jmp .eus_ind_start_set
        .eus_ind_start_zero:
        xor ax, ax
        .eus_ind_start_set:
        test ax, ax
        jnz .eus_partial_ind
        ;; Full free: use ext2_free_ind_block (data blocks + indirect block itself)
        mov ax, [ext2_us_ind_blk]
        call ext2_free_ind_block
        jc .eus_err
        jmp .eus_doubly
        .eus_partial_ind:
        ;; Partial free: iterate from ind_start with index-based re-reads
        mov [ext2_us_cur_ptr], ax           ; flat index (= ind_start)
        xor cx, cx
        mov cl, [ext2_log_block_size]
        mov ax, 256
        shl ax, cl                          ; AX = ptrs_per_blk
        mov [ext2_us_ind_secs], ax          ; repurposed: ptrs_per_blk
        .eus_partial_loop:
        mov ax, [ext2_us_cur_ptr]
        cmp ax, [ext2_us_ind_secs]
        jae .eus_doubly
        mov bx, ax
        shr bx, 7
        mov ax, [ext2_us_ind_blk]
        call ext2_read_blk_sec
        jc .eus_err
        mov bx, [ext2_us_cur_ptr]
        and bx, 07Fh
        shl bx, 2
        mov ax, [SECTOR_BUFFER + bx]
        test ax, ax
        jz .eus_partial_next
        call ext2_free_block
        jc .eus_err
        .eus_partial_next:
        inc word [ext2_us_cur_ptr]
        jmp .eus_partial_loop
        .eus_doubly:
        ;; Handle doubly-indirect block i_block[13]
        mov ax, [ext2_us_blks + 26]
        test ax, ax
        jz .eus_done
        ;; Compute ptrs_per_blk; skip if partial doubly-indirect needed
        xor cx, cx
        mov cl, [ext2_log_block_size]
        mov ax, 256
        shl ax, cl                          ; AX = ptrs_per_blk
        mov cx, ax
        mov dx, [ext2_us_keep_blocks]
        cmp dx, 12
        jbe .eus_dbl_full
        sub dx, 12
        cmp dx, cx
        ja .eus_done                        ; keep_blocks > 12 + ptrs_per_blk: partial, skip
        .eus_dbl_full:
        mov [ext2_us_ind_secs], cx          ; ptrs_per_blk for doubly loop
        mov [ext2_us_cur_sec], ax           ; doubly-indirect block number
        mov word [ext2_us_cur_ptr], 0
        .eus_dbl_loop:
        mov ax, [ext2_us_cur_ptr]
        cmp ax, [ext2_us_ind_secs]
        jae .eus_dbl_free_self
        mov bx, ax
        shr bx, 7
        mov ax, [ext2_us_cur_sec]
        call ext2_read_blk_sec
        jc .eus_err
        mov bx, [ext2_us_cur_ptr]
        and bx, 07Fh
        shl bx, 2
        mov ax, [SECTOR_BUFFER + bx]
        test ax, ax
        jz .eus_dbl_next
        call ext2_free_ind_block
        jc .eus_err
        .eus_dbl_next:
        inc word [ext2_us_cur_ptr]
        jmp .eus_dbl_loop
        .eus_dbl_free_self:
        mov ax, [ext2_us_cur_sec]
        call ext2_free_block
        jc .eus_err
        .eus_grow:
        mov ax, [si+FD_OFFSET_POSITION]
        mov [bx + EXT2_INODE_SIZE_LO], ax
        mov ax, [si+FD_OFFSET_POSITION+2]
        mov [bx + EXT2_INODE_SIZE_LO + 2], ax
        mov ax, [ext2_last_blk_sec]
        call write_sector
        jc .eus_err
        .eus_done:
        .eus_no_update:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        clc
        ret
        .eus_err:
        pop di
        pop si
        pop dx
        pop cx
        pop bx
        pop ax
        stc
        ret

ext2_write_blk_sec:
        ;; Write SECTOR_BUFFER back to the sector cached in ext2_last_blk_sec.
        ;; Output: CF on disk error
        push ax
        mov ax, [ext2_last_blk_sec]
        call write_sector
        pop ax
        ret

ext2_free_ind_block:
        ;; Free all data blocks referenced by singly-indirect block AX, then free AX.
        ;; AX = 0 is a no-op (CF clear). Clobbers: AX, BX (saved/restored).
        push bx
        test ax, ax
        jz .efib_done
        mov [ext2_fib_blk], ax
        xor cx, cx
        mov cl, [ext2_log_block_size]
        mov ax, 256
        shl ax, cl                      ; AX = ptrs_per_blk = 256 << log_block_size
        mov [ext2_fib_count], ax
        mov word [ext2_fib_idx], 0
        .efib_loop:
        mov ax, [ext2_fib_idx]
        cmp ax, [ext2_fib_count]
        jae .efib_free_self
        ;; Re-read sector each time (SECTOR_BUFFER clobbered by ext2_free_block)
        mov bx, ax
        shr bx, 7                       ; BX = sector within indirect block
        mov ax, [ext2_fib_blk]
        call ext2_read_blk_sec          ; AX=block, BX=sector → SECTOR_BUFFER
        jc .efib_err
        mov bx, [ext2_fib_idx]
        and bx, 07Fh
        shl bx, 2                       ; BX = byte offset within sector
        mov ax, [SECTOR_BUFFER + bx]
        test ax, ax
        jz .efib_next
        call ext2_free_block
        jc .efib_err
        .efib_next:
        inc word [ext2_fib_idx]
        jmp .efib_loop
        .efib_free_self:
        mov ax, [ext2_fib_blk]
        call ext2_free_block
        jc .efib_err
        .efib_done:
        pop bx
        clc
        ret
        .efib_err:
        pop bx
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
        ;; Clobbers: AX, BX, CX, DX
        cmp ax, 12
        jb .direct
        sub ax, 12                      ; AX = idx within indirect region
        ;; DX = ptrs_per_blk = 256 << log_block_size
        xor cx, cx
        mov cl, [ext2_log_block_size]
        mov dx, 256
        shl dx, cl                      ; DX = ptrs_per_blk
        cmp ax, dx
        jae .doubly
        ;; --- Singly indirect: i_block[12] ---
        ;; entry offset = (ax & 0x7F) * 4, sector = ax >> 7
        mov cx, ax
        and cx, 07Fh
        shl cx, 2                       ; CX = byte offset of entry within sector
        shr ax, 7                       ; AX = sector within indirect block
        push cx                         ; save entry_offset
        add bx, EXT2_INODE_BLOCK + 48   ; BX = &i_block[12]
        mov cx, [bx]                    ; CX = indirect block pointer
        mov bx, ax                      ; BX = sector_in_indirect_block
        mov ax, cx                      ; AX = indirect block pointer
        call ext2_read_blk_sec
        jc .singly_err
        pop bx                          ; BX = entry_offset
        mov ax, [SECTOR_BUFFER + bx]
        clc
        ret
        .singly_err:
        add sp, 2
        stc
        ret
        ;; --- Doubly indirect: i_block[13] ---
        .doubly:
        sub ax, dx                      ; AX = dbl_idx (within doubly-indirect region)
        ;; outer_idx = dbl_idx / ptrs_per_blk; inner_idx = dbl_idx % ptrs_per_blk
        ;; ptrs_per_blk = 256 << log_block_size; log2(ptrs_per_blk) = 8 + log_block_size
        mov [ext2_gdb_ptrs], dx         ; save ptrs_per_blk
        xor cx, cx
        mov cl, [ext2_log_block_size]
        add cl, 8                       ; CL = log2(ptrs_per_blk)
        mov dx, ax
        shr dx, cl                      ; DX = outer_idx
        mov cx, [ext2_gdb_ptrs]
        dec cx                          ; CX = ptrs_per_blk - 1
        and ax, cx                      ; AX = inner_idx
        mov [ext2_gdb_inner], ax        ; save inner_idx
        ;; Read i_block[13] (doubly-indirect block) from inode
        add bx, EXT2_INODE_BLOCK + 52   ; 13 * 4 = 52
        mov cx, [bx]                    ; CX = doubly-indirect block number
        ;; Outer lookup: sector = DX >> 7, offset = (DX & 0x7F) * 4
        mov bx, dx
        shr bx, 7                       ; BX = sector within doubly-indirect block
        and dx, 07Fh
        shl dx, 2                       ; DX = byte offset within sector
        push dx                         ; save outer byte offset
        mov ax, cx                      ; AX = doubly-indirect block number
        call ext2_read_blk_sec
        jc .dbl_err
        pop bx                          ; BX = outer byte offset
        mov cx, [SECTOR_BUFFER + bx]    ; CX = singly-indirect block number
        ;; Inner lookup: sector = inner_idx >> 7, offset = (inner_idx & 0x7F) * 4
        mov ax, [ext2_gdb_inner]        ; AX = inner_idx
        mov bx, ax
        shr bx, 7                       ; BX = sector within singly-indirect block
        and ax, 07Fh
        shl ax, 2                       ; AX = byte offset
        push ax                         ; save inner byte offset
        mov ax, cx                      ; AX = singly-indirect block number
        call ext2_read_blk_sec
        jc .dbl_err2
        pop bx                          ; BX = inner byte offset
        mov ax, [SECTOR_BUFFER + bx]    ; AX = data block number
        clc
        ret
        .dbl_err2:
        add sp, 2                       ; discard inner byte offset
        .dbl_err:
        stc
        ret
        .direct:
        shl ax, 2
        add bx, EXT2_INODE_BLOCK
        add bx, ax
        mov ax, [bx]
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
        ;; Input: AX = block number, BX = sector offset within block (0-based)
        ;; Output: CF set on error; ext2_last_blk_sec set for write-back
        ;; Clobbers: AX
        push cx
        xor cx, cx
        mov cl, [ext2_log_block_size]
        inc cl                          ; sectors_per_block = 2^(log+1)
        shl ax, cl                      ; AX = first disk sector of block (relative)
        add ax, EXT2_START_SECTOR
        add ax, bx
        mov [ext2_last_blk_sec], ax
        call read_sector
        pop cx
        ret

ext2_read_inode:
        ;; Read inode AX into SECTOR_BUFFER; return BX = pointer to inode
        ;; Also sets ext2_last_read_inode and ext2_last_blk_sec for write-back
        ;; Clobbers: AX, BX, CX, DX
        push si
        mov [ext2_last_read_inode], ax
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
        ;; DX = sectors_per_block = 1 << (log_block_size + 1)
        xor ch, ch
        mov cl, [ext2_log_block_size]
        inc cl
        mov dx, 1
        shl dx, cl
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
        ;; Decompose 32-bit position into byte_in_sector, sector_in_block, block_index
        mov ax, [si+FD_OFFSET_POSITION]
        mov dx, [si+FD_OFFSET_POSITION+2]
        mov bx, ax
        and bx, 01FFh           ; BX = byte offset within sector (to return)
        ;; sector_index = pos >> 9 (fits in 16 bits for files < 32 MB)
        shr ax, 9
        shl dx, 7
        or ax, dx               ; AX = sector_index
        ;; block_index = sector_index >> (log+1); sector_in_block = sector_index & (spb-1)
        xor ch, ch
        mov cl, [ext2_log_block_size]
        inc cl                  ; cl = log+1 = log2(sectors_per_block)
        push cx                 ; save log+1
        mov dx, ax              ; DX = sector_index
        shr ax, cl              ; AX = block_index
        pop cx                  ; cl = log+1 (restore)
        push ax                 ; save block_index
        mov ax, 1
        shl ax, cl              ; AX = sectors_per_block
        dec ax                  ; AX = sectors_per_block - 1 (mask)
        and dx, ax              ; DX = sector_in_block
        mov cx, dx              ; CX = sector_in_block
        pop ax                  ; AX = block_index
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

ext2_resolve_path:
        ;; Parse a path into (parent_dir_inode, basename).
        ;; Input:  SI = null-terminated path (optionally "dir/name")
        ;; Output: AX = parent dir inode, SI = basename; CF if parent dir not found
        push cx
        push di
        mov di, si
        .erp_scan:
        cmp byte [di], 0
        je .erp_root
        cmp byte [di], '/'
        je .erp_subdir
        inc di
        jmp .erp_scan
        .erp_root:
        mov ax, EXT2_ROOT_INODE
        pop di
        pop cx
        clc
        ret
        .erp_subdir:
        mov byte [di], 0                ; null-terminate dirname
        push di                         ; save slash position
        mov ax, EXT2_ROOT_INODE
        call ext2_search_dir            ; AX=root, SI=dirname → AX=dir_inode; CF if not found
        pop di                          ; DI = slash position
        mov byte [di], '/'              ; restore slash
        jc .erp_not_found
        inc di                          ; DI = basename
        mov si, di
        pop di                          ; restore caller's DI
        pop cx
        clc
        ret
        .erp_not_found:
        pop di
        pop cx
        stc
        ret

        ;; State
        ext2_ade_cur_blk       dw 0     ; ext2_add_dir_entry: current block index
        ext2_ade_filetype      db 1     ; ext2_add_dir_entry: file type (1=reg, 2=dir)
        ext2_ade_inode         dw 0     ; ext2_add_dir_entry: new file's inode
        ext2_ade_min_rec       dw 0     ; ext2_add_dir_entry: minimum rec_len needed
        ext2_ade_name          dw 0     ; ext2_add_dir_entry: pointer to name string
        ext2_ade_namelen       dw 0     ; ext2_add_dir_entry: name length in bytes
        ext2_ade_new_blk       dw 0     ; ext2_add_dir_entry: newly allocated block number
        ext2_alloc_bitmap_blk  dw 0     ; ext2_alloc_bit: bitmap block being scanned
        ext2_bgd_block         dw 0
        ext2_block_bitmap_blk  dw 0
        ext2_first_data_block  dw 0
        ext2_cr_mode           db 0     ; ext2_create: FLAG_EXECUTE / FLAG_DIRECTORY
        ext2_cr_name           dw 0     ; ext2_create: pointer to filename
        ext2_cr_new_inode      dw 0     ; ext2_create: allocated inode number
        ext2_cr_parent_inode   dw 0     ; ext2_create: parent directory inode
        ext2_dir_blks          times 12 dw 0
        ext2_dl_blks           times 14 dw 0  ; ext2_delete: saved i_block[0..13]
        ext2_dl_dbl_blk        dw 0     ; ext2_delete: doubly-indirect block number
        ext2_dl_dbl_count      dw 0     ; ext2_delete: ptrs_per_blk for doubly-indirect scan
        ext2_dl_dbl_idx        dw 0     ; ext2_delete: current index in doubly-indirect block
        ext2_dl_dtime_hi       dw 0     ; ext2_delete: i_dtime epoch (high 16)
        ext2_dl_dtime_lo       dw 0     ; ext2_delete: i_dtime epoch (low 16)
        ext2_dl_inode          dw 0     ; ext2_delete: inode number to free
        ext2_dl_name           dw 0     ; ext2_delete: pointer to basename
        ext2_dl_parent_inode   dw 0     ; ext2_delete: parent directory inode
        ext2_fb_bitmap_blk     dw 0     ; ext2_free_bit: bitmap block being cleared
        ext2_fib_blk           dw 0     ; ext2_free_ind_block: indirect block number
        ext2_fib_count         dw 0     ; ext2_free_ind_block: ptrs_per_blk
        ext2_fib_idx           dw 0     ; ext2_free_ind_block: current pointer index
        ext2_gdb_inner         dw 0     ; ext2_get_data_block: inner index for doubly-indirect
        ext2_gdb_ptrs          dw 0     ; ext2_get_data_block: ptrs_per_blk for doubly-indirect
        ext2_inode_bitmap_blk  dw 0
        ext2_inode_size        dw 128
        ext2_inode_table_blk   dw 0
        ext2_inodes_per_group  dw 0
        ext2_last_blk_sec      dw 0
        ext2_last_read_inode   dw 0
        ext2_load_blk_counter  dw 0
        ext2_load_blks         times 12 dw 0
        ext2_load_dbl_ptr      dw 0     ; ext2_load: i_block[13] (doubly-indirect pointer)
        ext2_load_indirect_ptr dw 0
        ext2_load_ptrs         dw 0     ; ext2_load: ptrs_per_blk
        ext2_load_rem          dw 0
        ext2_log_block_size    db 0
        ext2_mk_name           dw 0     ; ext2_mkdir: pointer to basename
        ext2_mk_new_blk        dw 0     ; ext2_mkdir: newly allocated data block
        ext2_mk_new_inode      dw 0     ; ext2_mkdir: newly allocated inode
        ext2_mk_parent_inode   dw 0     ; ext2_mkdir: parent directory inode
        ext2_pws_block_idx     dw 0     ; ext2_prepare_write_sec: block index
        ext2_pws_byte_offset   dw 0     ; ext2_prepare_write_sec: byte offset within sector
        ext2_pws_ind_blk       dw 0     ; ext2_prepare_write_sec: indirect block number
        ext2_pws_ptr_idx       dw 0     ; ext2_prepare_write_sec: entry index in indirect block
        ext2_pws_sec_in_blk    dw 0     ; ext2_prepare_write_sec: sector within block
        ext2_rd_inode          dw 0
        ext2_rd_name           times DIRECTORY_NAME_LENGTH db 0
        ext2_rd_outbuf         dw 0
        ext2_rd_rec_len        dw 0
        ext2_rde_name          dw 0     ; ext2_remove_dir_entry: pointer to name string
        ext2_rdr_blks          times 14 dw 0  ; ext2_rmdir: saved i_block[0..13]
        ext2_rdr_cde_blk       dw 0     ; ext2_check_dir_empty: block number
        ext2_rdr_dbl_blk       dw 0     ; ext2_rmdir: doubly-indirect block number
        ext2_rdr_dbl_count     dw 0     ; ext2_rmdir: ptrs_per_blk for doubly-indirect scan
        ext2_rdr_dbl_idx       dw 0     ; ext2_rmdir: current index in doubly-indirect block
        ext2_rdr_dtime_hi      dw 0     ; ext2_rmdir: i_dtime epoch (high 16)
        ext2_rdr_dtime_lo      dw 0     ; ext2_rmdir: i_dtime epoch (low 16)
        ext2_rdr_inode         dw 0     ; ext2_rmdir: inode number to free
        ext2_rdr_name          dw 0     ; ext2_rmdir: pointer to basename
        ext2_rdr_parent_inode  dw 0     ; ext2_rmdir: parent directory inode
        ext2_rn_new_dir        dw 0     ; ext2_rename: new parent dir inode
        ext2_rn_new_name       dw 0     ; ext2_rename: pointer to new basename
        ext2_rn_new_path       dw 0     ; ext2_rename: pointer to new full path
        ext2_rn_old_dir        dw 0     ; ext2_rename: old parent dir inode
        ext2_rn_old_inode      dw 0     ; ext2_rename: inode to relocate
        ext2_rn_old_name       dw 0     ; ext2_rename: pointer to old basename
        ext2_rn_old_path       dw 0     ; ext2_rename: pointer to old full path
        ext2_sd_name           dw 0
        ext2_us_blks           times 14 dw 0  ; ext2_update_size: saved i_block[0..13]
        ext2_us_cur_ptr        dw 0     ; ext2_update_size: pointer index within current sector
        ext2_us_cur_sec        dw 0     ; ext2_update_size: current sector in indirect block
        ext2_us_fd             dw 0     ; ext2_update_size: fd_entry pointer
        ext2_us_ind_blk        dw 0     ; ext2_update_size: indirect block number
        ext2_us_ind_fptr       dw 0     ; ext2_update_size: first pointer index in first sector
        ext2_us_ind_fsec       dw 0     ; ext2_update_size: first sector index to process
        ext2_us_ind_secs       dw 0     ; ext2_update_size: sectors_per_block for indirect scan
        ext2_us_keep_blocks    dw 0     ; ext2_update_size: ceil(new_size / block_size)
