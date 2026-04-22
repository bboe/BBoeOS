;;; vfs.asm -- Virtual Filesystem Switch
;;;
;;; A table of function pointers that dispatch to the active filesystem
;;; implementation.  Swap the pointer values to change filesystems without
;;; touching any caller.
;;;
;;; vfs_chmod:       SI=path, AL=mode → CF on error, AL=error code
;;; vfs_create:      SI=path → vfs_found_*, CF on error
;;; vfs_find:        SI=path → vfs_found_*, CF if not found
;;; vfs_init:        detect filesystem; swap pointers to ext2 if magic matches
;;; vfs_load:        DI=dest → CF (loads file using vfs_found_inode+vfs_found_size)
;;; vfs_mkdir:       SI=name → AX=inode, CF on error
;;; vfs_rename:      SI=old, DI=new → CF on error, AL=error code
;;; vfs_update_size: SI=fd_entry → CF on disk error

;;; Function pointer table — one word per VFS operation (no entry for vfs_init:
;;; init runs once at boot and swaps these pointers; it need not be swappable)
vfs_chmod_fn       dw bbfs_chmod
vfs_create_fn      dw bbfs_create
vfs_find_fn        dw bbfs_find
vfs_load_fn        dw bbfs_load
vfs_mkdir_fn       dw bbfs_mkdir
vfs_rename_fn      dw bbfs_rename
vfs_update_size_fn dw bbfs_update_size

;;; State populated by vfs_find / vfs_create, consumed by fd_open and sys_exec
vfs_found_dir_off  dw 0     ; byte offset of entry within its directory sector
vfs_found_dir_sec  dw 0     ; directory sector containing this entry
vfs_found_inode    dw 0     ; start sector (bbfs) or inode number (ext2)
vfs_found_mode     db 0     ; FLAG_EXECUTE / FLAG_DIRECTORY (bbfs) or i_mode bits (ext2)
vfs_found_size     dd 0     ; file size (32-bit, little-endian)
vfs_found_type     db 0     ; FD_TYPE_FILE or FD_TYPE_DIRECTORY

vfs_chmod:       jmp [vfs_chmod_fn]
vfs_create:      jmp [vfs_create_fn]
vfs_find:        jmp [vfs_find_fn]
vfs_load:        jmp [vfs_load_fn]
vfs_mkdir:       jmp [vfs_mkdir_fn]
vfs_rename:      jmp [vfs_rename_fn]
vfs_update_size: jmp [vfs_update_size_fn]

vfs_init:
        ;; Detect the active filesystem and set function pointers accordingly.
        ;; Tries ext2 first (reads superblock magic); falls back to bbfs.
        call ext2_init
        jc .bbfs
        ;; ext2 detected: swap read paths; write ops stay as stubs
        mov word [vfs_find_fn], ext2_find
        mov word [vfs_load_fn], ext2_load
        mov word [vfs_chmod_fn], ext2_readonly
        mov word [vfs_create_fn], ext2_readonly
        mov word [vfs_mkdir_fn], ext2_readonly
        mov word [vfs_rename_fn], ext2_readonly
        mov word [vfs_update_size_fn], ext2_readonly
        ret
        .bbfs:
        call bbfs_init          ; no-op, but keeps the call site explicit
        ret

%include "fs/bbfs.asm"
%include "fs/ext2.asm"
