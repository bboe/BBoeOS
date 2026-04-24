;;; vfs.asm -- Virtual Filesystem Switch
;;;
;;; A table of function pointers that dispatch to the active filesystem
;;; implementation.  Swap the pointer values to change filesystems without
;;; touching any caller.
;;;
;;; vfs_chmod:              SI=path, AL=mode → CF on error, AL=error code
;;; vfs_commit_write_sec:   SI=fd_entry → CF on disk error (write SECTOR_BUFFER to cached sector)
;;; vfs_create:             SI=path → vfs_found_*, CF on error
;;; vfs_delete:             SI=path → CF on error, AL=error code
;;; vfs_find:               SI=path → vfs_found_*, CF if not found
;;; vfs_init:               detect filesystem; swap pointers to ext2 if magic matches
;;; vfs_load:               DI=dest → CF (loads file using vfs_found_inode+vfs_found_size)
;;; vfs_mkdir:              SI=name → AX=inode, CF on error
;;; vfs_prepare_write_sec:  SI=fd_entry → SECTOR_BUFFER filled, BX=byte offset; CF on err
;;; vfs_rmdir:              SI=name → CF on error, AL=error code
;;; vfs_read_dir:           SI=fd_entry, DI=buf → AX=bytes (DIRECTORY_ENTRY_SIZE or 0); CF on err
;;; vfs_read_sec:           SI=fd_entry → SECTOR_BUFFER filled, BX=byte offset; CF on err
;;; vfs_rename:             SI=old, DI=new → CF on error, AL=error code
;;; vfs_update_size:        SI=fd_entry → CF on disk error

;;; Function pointer table — one word per VFS operation (no entry for vfs_init:
;;; init runs once at boot and swaps these pointers; it need not be swappable)
vfs_chmod_fn              dw bbfs_chmod
vfs_commit_write_sec_fn   dw bbfs_commit_write_sec  ; SI=fd_entry → CF on err
vfs_create_fn             dw bbfs_create
vfs_delete_fn             dw bbfs_delete
vfs_find_fn               dw bbfs_find
vfs_load_fn               dw bbfs_load
vfs_mkdir_fn              dw bbfs_mkdir
vfs_prepare_write_sec_fn  dw bbfs_prepare_write_sec ; SI=fd_entry → SECTOR_BUFFER, BX=byte offset; CF on err
vfs_read_dir_fn           dw bbfs_read_dir           ; SI=fd_entry, DI=buf → AX=bytes; CF on err
vfs_read_sec_fn           dw bbfs_read_sec            ; SI=fd entry → SECTOR_BUFFER filled, BX=byte offset; CF on err
vfs_rename_fn             dw bbfs_rename
vfs_rmdir_fn              dw bbfs_rmdir
vfs_update_size_fn        dw bbfs_update_size

;;; State populated by vfs_find / vfs_create, consumed by fd_open and sys_exec
vfs_found_dir_off  dw 0     ; byte offset of entry within its directory sector
vfs_found_dir_sec  dw 0     ; directory sector containing this entry
vfs_found_inode    dw 0     ; start sector (bbfs) or inode number (ext2)
vfs_found_mode     db 0     ; FLAG_EXECUTE / FLAG_DIRECTORY (bbfs) or i_mode bits (ext2)
vfs_found_size     dd 0     ; file size (32-bit, little-endian)
vfs_found_type     db 0     ; FD_TYPE_FILE or FD_TYPE_DIRECTORY

vfs_chmod:             jmp [vfs_chmod_fn]
vfs_commit_write_sec:  jmp [vfs_commit_write_sec_fn]
vfs_create:            jmp [vfs_create_fn]
vfs_delete:            jmp [vfs_delete_fn]
vfs_find:              jmp [vfs_find_fn]
vfs_load:              jmp [vfs_load_fn]
vfs_mkdir:             jmp [vfs_mkdir_fn]
vfs_prepare_write_sec: jmp [vfs_prepare_write_sec_fn]
vfs_read_dir:          jmp [vfs_read_dir_fn]
vfs_read_sec:          jmp [vfs_read_sec_fn]
vfs_rename:            jmp [vfs_rename_fn]
vfs_rmdir:             jmp [vfs_rmdir_fn]
vfs_update_size:       jmp [vfs_update_size_fn]

vfs_init:
        ;; Detect the active filesystem and set function pointers accordingly.
        ;; Tries ext2 first (reads superblock magic); falls back to bbfs.
        call ext2_init
        jc .bbfs
        ;; ext2 detected: swap all paths
        mov word [vfs_find_fn], ext2_find
        mov word [vfs_load_fn], ext2_load
        mov word [vfs_read_dir_fn], ext2_read_dir
        mov word [vfs_read_sec_fn], ext2_read_sec
        mov word [vfs_chmod_fn], ext2_chmod
        mov word [vfs_commit_write_sec_fn], ext2_commit_write_sec
        mov word [vfs_create_fn], ext2_create
        mov word [vfs_delete_fn], ext2_delete
        mov word [vfs_mkdir_fn], ext2_mkdir
        mov word [vfs_prepare_write_sec_fn], ext2_prepare_write_sec
        mov word [vfs_rename_fn], ext2_rename
        mov word [vfs_rmdir_fn], ext2_rmdir
        mov word [vfs_update_size_fn], ext2_update_size
        ret
        .bbfs:
        call bbfs_init          ; no-op, but keeps the call site explicit
        ret

%include "fs/bbfs.asm"
%include "fs/ext2.asm"
