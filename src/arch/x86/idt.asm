;;; ------------------------------------------------------------------------
;;; idt.asm — 32-bit IDT with exception stubs for protected-mode use.
;;;
;;; Exports a statically-built IDT covering vectors 0..31 (CPU exceptions)
;;; and 0x30 (INT 30h syscall gate).  Each exception stub normalizes the
;;; stack (pushes a fake error code when the CPU didn't), pushes the
;;; exception number, and jumps to exc_common which prints
;;; "EXCnn EIP=... CR2=... ERR=..." on COM1, then triages:
;;;   * CPL=3 (any vector) → tear down the dying program's PD and
;;;     re-enter shell_reload, mirroring sys_exit's teardown sequence.
;;;   * CPL=0 + #PF + CR2 < 0xC0000000 → also kill.  The kernel was
;;;     dereferencing a user pointer mid-syscall (e.g. read() into an
;;;     unmapped user buffer); the program is the bug, not the kernel.
;;;     Phase 5 PR B's access_ok will reject these at the syscall
;;;     boundary, but until then this route keeps the kernel alive.
;;;   * Anything else → halt.  Kernel-half CR2 on #PF, or any non-#PF
;;;     exception at CPL=0, is a kernel bug we want loud.
;;;
;;; The IDTR is loaded via `lidt [idtr]` in `kernel.asm`'s `high_entry`,
;;; right after the boot far-jump lands at the high-half kernel.
;;; Any fault from that point vectors through our stubs.  PIC remap is
;;; orthogonal (belongs with the CR0.PE flip in boot.asm); this module
;;; does not touch the PICs.
;;; ------------------------------------------------------------------------

        IDT_CODE_SELECTOR       equ 08h          ; flat 32-bit code (protected mode GDT[1])
        IDT_FLAGS_INT32         equ 8Eh          ; P=1 DPL=0 type=0xE
        IDT_FLAGS_INT32_DPL3    equ 0EEh         ; P=1 DPL=3 type=0xE — ring-3 callable (INT 30h)
        LSR_THRE                equ 20h

%macro IDT_ENTRY 1
        ;; Low 16 bits of the handler offset are emitted by NASM's
        ;; native ``dw symbol`` truncation.  The high 16 bits are left
        ;; zero here and patched at boot by ``idt_init`` — NASM's
        ;; ``bin`` format treats labels as section-relative so the
        ;; bitwise arithmetic that would otherwise produce them
        ;; rejects with "may only be applied to scalar values" even
        ;; for backward references.  When the kernel lives entirely
        ;; in the low 64 KB (current pre-paging boot), the high half
        ;; is already zero and ``idt_init`` is a no-op; once the
        ;; kernel relocates above that line, the patch lights up the
        ;; high half automatically.
        dw %1
        dw IDT_CODE_SELECTOR
        db 0
        db IDT_FLAGS_INT32
        dw 0
%endmacro

%macro EXC_NOERR 1
exc_%1:
        push 0                      ; fake error code
        push %1
        jmp exc_common
%endmacro

%macro EXC_ERR 1
exc_%1:
        push %1                     ; CPU already pushed the real error code
        jmp exc_common
%endmacro

        ;; Exception stub table.  Error-code exceptions on 386+: 8, 10..14, 17.
        EXC_NOERR 0
        EXC_NOERR 1
        EXC_NOERR 2
        EXC_NOERR 3
        EXC_NOERR 4
        EXC_NOERR 5
        EXC_NOERR 6
        EXC_NOERR 7
        EXC_ERR   8
        EXC_NOERR 9
        EXC_ERR   10
        EXC_ERR   11
        EXC_ERR   12
        EXC_ERR   13
        EXC_ERR   14
        EXC_NOERR 15
        EXC_NOERR 16
        EXC_ERR   17
        EXC_NOERR 18
        EXC_NOERR 19
        EXC_NOERR 20
        EXC_NOERR 21
        EXC_NOERR 22
        EXC_NOERR 23
        EXC_NOERR 24
        EXC_NOERR 25
        EXC_NOERR 26
        EXC_NOERR 27
        EXC_NOERR 28
        EXC_NOERR 29
        EXC_NOERR 30
        EXC_NOERR 31

exc_common:
        ;; Stack on entry (top → bottom):
        ;;   [esp+0]  exception number (our push)
        ;;   [esp+4]  error code (real or faked 0)
        ;;   [esp+8]  EIP
        ;;   [esp+12] CS
        ;;   [esp+16] EFLAGS
        ;; We never return — just announce and halt.
        mov al, 'E'
        call exc_putc
        mov al, 'X'
        call exc_putc
        mov al, 'C'
        call exc_putc
        mov al, [esp]                   ; exception number
        shr al, 4
        call exc_puthex
        mov al, [esp]
        and al, 0Fh
        call exc_puthex
        ;; Print " EIP=hhhhhhhh CR2=hhhhhhhh ERR=hhhhhhhh".  Useful for
        ;; debugging page faults / GP faults from user programs and
        ;; from the kernel itself before the per-vector handlers in
        ;; arch/x86/exc.asm land.
        mov al, ' '
        call exc_putc
        mov al, 'E'
        call exc_putc
        mov al, 'I'
        call exc_putc
        mov al, 'P'
        call exc_putc
        mov al, '='
        call exc_putc
        mov eax, [esp + 8]              ; saved EIP
        call exc_puthex32
        mov al, ' '
        call exc_putc
        mov al, 'C'
        call exc_putc
        mov al, 'R'
        call exc_putc
        mov al, '2'
        call exc_putc
        mov al, '='
        call exc_putc
        mov eax, cr2
        call exc_puthex32
        mov al, ' '
        call exc_putc
        mov al, 'E'
        call exc_putc
        mov al, 'R'
        call exc_putc
        mov al, 'R'
        call exc_putc
        mov al, '='
        call exc_putc
        mov eax, [esp + 4]              ; error code
        call exc_puthex32
        mov al, 0Dh
        call exc_putc
        mov al, 0Ah
        call exc_putc

        ;; Triage.  exc_putc / exc_puthex / exc_puthex32 all preserve the
        ;; stack, so the iret frame is still where it was on entry:
        ;;   [esp+0]  exception number   [esp+12] CS
        ;;   [esp+4]  error code         [esp+16] EFLAGS
        ;;   [esp+8]  EIP
        ;; Dispatch:
        ;;   CPL=3 (user-mode fault, any vector) → kill program.
        ;;   CPL=0 + #PF + CR2 < 0xC0000000      → kill program.  The kernel
        ;;       was dereferencing a user pointer mid-syscall (e.g. read()
        ;;       into an unmapped user buffer).  Phase 5 PR B's access_ok
        ;;       will reject these at the syscall boundary, but until then
        ;;       routing the fault through the kill path keeps the kernel
        ;;       alive instead of bricking on every syscall with a bad
        ;;       user pointer.
        ;;   Anything else                        → halt.  Kernel-half CR2
        ;;       on #PF, or any non-#PF exception at CPL=0, is a kernel
        ;;       bug we want loud.
        test byte [esp + 12], 3
        jnz .kill_program
        cmp dword [esp], 14
        jne .halt_kernel
        mov eax, cr2
        cmp eax, 0xC0000000
        jae .halt_kernel

        .kill_program:
        ;; Tear down the dying program's PD and re-enter shell_reload.
        ;; CR3 still points at the dying program's PD (the CPU doesn't
        ;; change CR3 on a fault).  Mirrors sys_exit (src/syscall/sys.asm).
        ;; We never return through the iret frame — the program's death
        ;; is final, including for the kernel-deref-of-user-pointer case
        ;; (whatever kernel-side state the syscall built up is abandoned;
        ;; fd_init in shell_reload re-zeroes the FD table for the next
        ;; program, and the dying program's user pages go away with the
        ;; PD).
        mov eax, cr3
        push eax
        mov eax, [kernel_pd_template_phys]
        mov cr3, eax
        pop eax
        call address_space_destroy
        mov esp, [shell_esp]
        sti
        jmp shell_reload

        .halt_kernel:
        cli
        .halt:
        hlt
        jmp .halt

exc_putc:
        ;; AL = char.  Writes to COM1 after polling LSR.THRE.  Preserves
        ;; EAX and EDX.
        push eax
        push edx
        mov ah, al
        .wait:
        mov dx, COM1_LSR
        in al, dx
        test al, LSR_THRE
        jz .wait
        mov dx, COM1_DATA
        mov al, ah
        out dx, al
        pop edx
        pop eax
        ret

exc_puthex:
        ;; Low nibble of AL → ASCII hex digit → COM1.
        and al, 0Fh
        cmp al, 10
        jb .digit
        add al, 'A' - 10 - '0'
        .digit:
        add al, '0'
        jmp exc_putc

exc_puthex32:
        ;; EAX = 32-bit value, prints as 8 hex digits MSB-first.
        push eax
        push ecx
        mov ecx, 8
.next:
        rol eax, 4
        push eax
        and al, 0Fh
        call exc_puthex
        pop eax
        loop .next
        pop ecx
        pop eax
        ret

        ;; ----- IDT data -----
        ;; Vectors 0..31: CPU exceptions. Vectors 32..47: reserved for
        ;; remapped IRQs (filled in later by entry.asm via idt_set_gate32).
        align 8
idt_start:
        IDT_ENTRY exc_0
        IDT_ENTRY exc_1
        IDT_ENTRY exc_2
        IDT_ENTRY exc_3
        IDT_ENTRY exc_4
        IDT_ENTRY exc_5
        IDT_ENTRY exc_6
        IDT_ENTRY exc_7
        IDT_ENTRY exc_8
        IDT_ENTRY exc_9
        IDT_ENTRY exc_10
        IDT_ENTRY exc_11
        IDT_ENTRY exc_12
        IDT_ENTRY exc_13
        IDT_ENTRY exc_14
        IDT_ENTRY exc_15
        IDT_ENTRY exc_16
        IDT_ENTRY exc_17
        IDT_ENTRY exc_18
        IDT_ENTRY exc_19
        IDT_ENTRY exc_20
        IDT_ENTRY exc_21
        IDT_ENTRY exc_22
        IDT_ENTRY exc_23
        IDT_ENTRY exc_24
        IDT_ENTRY exc_25
        IDT_ENTRY exc_26
        IDT_ENTRY exc_27
        IDT_ENTRY exc_28
        IDT_ENTRY exc_29
        IDT_ENTRY exc_30
        IDT_ENTRY exc_31
        ;; Vectors 32..47: reserved for IRQs after PIC remap.  Left as
        ;; not-present (all zeros) — a stray IRQ before they're filled in
        ;; will #GP, which lands in exc_common with exception 13.
        times (0x30 - 32) * 8 db 0
        ;; Vector 0x30 — INT 30h syscall gate.  DPL=3 so ring-3 programs
        ;; can issue `int 30h`.  Hardware IRQs and CPU exceptions ignore
        ;; gate DPL, so the lower 32 entries staying DPL=0 still works
        ;; while preventing ring 3 from synthesising fake fault frames.
        ;; Like the EXC entries above, the high half of the offset is
        ;; patched at boot by ``idt_init``.
        dw syscall_handler
        dw IDT_CODE_SELECTOR
        db 0
        db IDT_FLAGS_INT32_DPL3
        dw 0
idt_end:

idtr:
        dw idt_end - idt_start - 1
        dd idt_start

%macro IDT_PATCH 2
        ;; %1 = vector, %2 = handler symbol.  Writes high 16 bits of
        ;; the handler address into the IDT entry at offset +6.
        mov eax, %2
        shr eax, 16
        mov [idt_start + (%1) * 8 + 6], ax
%endmacro

idt_init:
        ;; Patch the offset[31:16] field of every statically-defined
        ;; IDT entry with the high half of its handler's address.
        ;; The IDT_ENTRY macro emits only the low half via NASM's
        ;; native ``dw symbol`` truncation (NASM's ``bin`` format
        ;; rejects ``& 0FFFFh`` / ``>> 16`` arithmetic on section-
        ;; relative labels, even backward references), so the runtime
        ;; patch is the only place high-half handler addresses get
        ;; written.  Idempotent — pre-paging the kernel lives in low
        ;; 64 KB so every patch writes 0 over an already-zero field;
        ;; once the kernel relocates above that line the high halves
        ;; light up automatically.
        push eax
        IDT_PATCH 0,  exc_0
        IDT_PATCH 1,  exc_1
        IDT_PATCH 2,  exc_2
        IDT_PATCH 3,  exc_3
        IDT_PATCH 4,  exc_4
        IDT_PATCH 5,  exc_5
        IDT_PATCH 6,  exc_6
        IDT_PATCH 7,  exc_7
        IDT_PATCH 8,  exc_8
        IDT_PATCH 9,  exc_9
        IDT_PATCH 10, exc_10
        IDT_PATCH 11, exc_11
        IDT_PATCH 12, exc_12
        IDT_PATCH 13, exc_13
        IDT_PATCH 14, exc_14
        IDT_PATCH 15, exc_15
        IDT_PATCH 16, exc_16
        IDT_PATCH 17, exc_17
        IDT_PATCH 18, exc_18
        IDT_PATCH 19, exc_19
        IDT_PATCH 20, exc_20
        IDT_PATCH 21, exc_21
        IDT_PATCH 22, exc_22
        IDT_PATCH 23, exc_23
        IDT_PATCH 24, exc_24
        IDT_PATCH 25, exc_25
        IDT_PATCH 26, exc_26
        IDT_PATCH 27, exc_27
        IDT_PATCH 28, exc_28
        IDT_PATCH 29, exc_29
        IDT_PATCH 30, exc_30
        IDT_PATCH 31, exc_31
        IDT_PATCH 0x30, syscall_handler
        pop eax
        ret

idt_set_gate32:
        ;; Install a 32-bit interrupt gate.  EAX = handler linear address,
        ;; BL = vector.  Writes offset_lo / selector=0x08 / reserved=0 /
        ;; flags=0x8E / offset_hi into `idt_start + vector*8`.  Callers
        ;; are expected to be in protected mode with the IDT live.  This
        ;; is the seam every widened IRQ / syscall handler uses to
        ;; register itself lazily from 32-bit code.
        push edi
        push eax
        movzx edi, bl
        shl edi, 3
        add edi, idt_start
        mov [edi], ax
        mov word [edi + 2], IDT_CODE_SELECTOR
        mov byte [edi + 4], 0
        mov byte [edi + 5], IDT_FLAGS_INT32
        shr eax, 16
        mov [edi + 6], ax
        pop eax
        pop edi
        ret

