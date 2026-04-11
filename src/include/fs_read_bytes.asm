fs_read_bytes:
        ;; Read one sector into DISK_BUFFER and return how many bytes
        ;; of it are inside the current file — the cc.py compiler
        ;; emits ``_fs_remaining`` and seeds it from fs_find's result
        ;; so callers see a POSIX read(2)-shaped return (bytes read,
        ;; or 0 at EOF / on disk error).
        ;; Input:  CX = logical sector number
        ;; Output: AX = bytes valid in DISK_BUFFER (0 = EOF or error)
        mov bx, [_fs_remaining]
        test bx, bx
        jz .done                ; already at EOF; return 0
        mov ah, SYS_FS_READ
        int 30h
        jc .done                ; disk error falls through to 'ret 0'
        mov ax, 512
        cmp bx, ax
        jae .full               ; full sector; leave AX = 512
        mov ax, bx              ; last sector: return the remaining count
.full:
        sub bx, ax
        mov [_fs_remaining], bx
        ret
.done:
        xor ax, ax
        ret
