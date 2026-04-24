syscall_handler:
        ;; Preserve the full user register file across every syscall.
        ;; ``pusha`` saves AX, CX, DX, BX, SP, BP, SI, DI (in that order),
        ;; so at [BP .. BP+14]:
        ;;   [BP+0]  DI [BP+2]  SI [BP+4]  BP [BP+6]  SP
        ;;   [BP+8]  BX [BP+10] DX [BP+12] CX [BP+14] AX
        ;; The iret frame from INT 30h sits above that:
        ;;   [BP+16] IP [BP+18] CS [BP+20] FLAGS
        ;; Handlers that return a value in AX (or CX/DX for fstat,
        ;; DX:AX for datetime) write it into the saved slot before
        ;; jumping to the common exit path, which pops back and
        ;; irets.  Net effect: the only register a caller treats as
        ;; clobbered is the one the ABI documents as "return value".
        pusha
        mov bp, sp

        cmp ah, SYS_FS_CHMOD   ; fs_chmod
        je .fs_chmod
        cmp ah, SYS_FS_MKDIR   ; fs_mkdir
        je .fs_mkdir
        cmp ah, SYS_FS_RENAME  ; fs_rename
        je .fs_rename
        cmp ah, SYS_FS_RMDIR   ; fs_rmdir
        je .fs_rmdir
        cmp ah, SYS_FS_UNLINK  ; fs_unlink
        je .fs_unlink

        cmp ah, SYS_IO_CLOSE   ; io_close
        je .io_close
        cmp ah, SYS_IO_FSTAT   ; io_fstat
        je .io_fstat
        cmp ah, SYS_IO_IOCTL   ; io_ioctl
        je .io_ioctl
        cmp ah, SYS_IO_OPEN    ; io_open
        je .io_open
        cmp ah, SYS_IO_READ    ; io_read
        je .io_read
        cmp ah, SYS_IO_WRITE   ; io_write
        je .io_write

        cmp ah, SYS_NET_MAC    ; net_mac
        je .net_mac
        cmp ah, SYS_NET_OPEN   ; net_open
        je .net_open
        cmp ah, SYS_NET_RECVFROM ; net_recvfrom
        je .net_recvfrom
        cmp ah, SYS_NET_SENDTO ; net_sendto
        je .net_sendto
        cmp ah, SYS_RTC_DATETIME ; rtc_datetime
        je .rtc_datetime
        cmp ah, SYS_RTC_MILLIS ; rtc_millis
        je .rtc_millis
        cmp ah, SYS_RTC_SLEEP  ; rtc_sleep
        je .rtc_sleep
        cmp ah, SYS_RTC_UPTIME ; rtc_uptime
        je .rtc_uptime

        cmp ah, SYS_EXEC       ; sys_exec
        je .sys_exec
        cmp ah, SYS_EXIT       ; sys_exit
        je .sys_exit
        cmp ah, SYS_REBOOT     ; sys_reboot
        je .sys_reboot
        cmp ah, SYS_SHUTDOWN   ; sys_shutdown
        je .sys_shutdown
        jmp .iret_done

%include "syscall/fs.asm"
%include "syscall/io.asm"
%include "syscall/net.asm"
%include "syscall/rtc.asm"
%include "syscall/sys.asm"

        .iret_cf:
        ;; Propagate the handler's CF to the caller's saved FLAGS,
        ;; write AX into the saved slot, then fall through to popa/iret.
        jnc .iret_cf_clear
        or word [bp+20], 0001h  ; Set CF in saved FLAGS
        jmp .iret_cf_write
        .iret_cf_clear:
        and word [bp+20], 0FFFEh ; Clear CF in saved FLAGS
        .iret_cf_write:
        mov [bp+14], ax         ; AX = return value / error code
        .iret_done:
        ;; Common exit: restore the full user register file and iret.
        popa
        iret

        .check_shell:
        ;; Returns ZF set if SI points to the shell path (null-terminated)
        push si
        push di
        push cx
        cld
        mov di, SHELL_NAME
        mov cx, 10             ; "bin/shell" + null terminator
        repe cmpsb
        pop cx
        pop di
        pop si
        ret

install_syscalls:
        ;; Install INT 30h handler
        push ax
        push bx
        push es
        xor ax, ax
        mov es, ax
        mov word [es:30h*4], syscall_handler
        mov word [es:30h*4+2], cs
        pop es
        pop bx
        pop ax
        ret

uptime_seconds:
        ;; Return AX = elapsed seconds since boot (low 16 bits of the
        ;; 32-bit result; EAX holds the full value).  Preserves ECX, EDX.
        push ecx
        push edx
        call rtc_tick_read      ; EAX = ticks since boot
        xor edx, edx
        mov ecx, TICKS_PER_SECOND
        div ecx                 ; EAX = elapsed seconds
        pop edx
        pop ecx
        ret

