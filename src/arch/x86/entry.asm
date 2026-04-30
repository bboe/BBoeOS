;;; ------------------------------------------------------------------------
;;; entry.asm — 32-bit post-flip kernel entry.
;;;
;;; protected_mode_entry runs once per boot — TSS / IRQ install, vDSO
;;; shared-frame allocation, driver / VFS / NIC init, banner — then
;;; falls through into shell_reload.
;;;
;;; shell_reload is the re-entry point for SYS_EXIT (after the dying
;;; program's PD has been torn down by sys_exit).  It loads bin/shell
;;; off disk into a kernel-side scratch buffer, zeroes the BUFFER /
;;; EXEC_ARG snapshots (a fresh shell inherits no args), and `jmp`s
;;; program_enter.
;;;
;;; program_enter builds a fresh per-program PD via address_space_create,
;;; populates the user-visible regions (program text + BSS, vDSO code
;;; page, user stack), restores BUFFER / EXEC_ARG into the new program's
;;; first user frame, snapshots the kernel ESP for sys_exit, switches
;;; CR3, and `iretd`s at CPL=3.
;;;
;;; Any CPU exception fired past this point vectors through `idt.asm`'s
;;; `exc_common` and prints `EXCnn` on COM1.  CPL=3 faults — and CPL=0
;;; #PFs whose CR2 lives in the user half (kernel was dereferencing a
;;; user pointer) — tear down the dying program's PD and jump back to
;;; shell_reload, mirroring sys_exit's teardown.  Anything else is a
;;; kernel bug and halts.
;;; ------------------------------------------------------------------------

        PMODE_IRQ0_VECTOR       equ 0x20        ; matches the pic_remap master base
        PMODE_IRQ6_VECTOR       equ 0x26

        ;; User-page PTE flag bundles.
        PTE_USER_RW             equ 0x107       ; P | RW | U
        PTE_USER_RX             equ 0x105       ; P | U (read-only — no NX in 32-bit non-PAE)
        PTE_USER_RX_SHARED      equ (PTE_USER_RX | ADDRESS_SPACE_PTE_SHARED)
        PTE_USER_RW_SHARED      equ (PTE_USER_RW | ADDRESS_SPACE_PTE_SHARED)

        ;; User address-space layout (Linux-shape, PROGRAM_BASE = 0x08048000):
        ;;   PTE 0x00000             : NOT MAPPED — NULL guard (deref → #PF)
        ;;   PTE 0x00001             : private — ARGV, EXEC_ARG, BUFFER (USER_DATA_BASE)
        ;;   PTE 0x00010             : shared  — vDSO code page (R-X)
        ;;   PTEs 0x08048..          : private — program text + BSS
        ;;   PTEs 0x3FFF0..0x3FFFF   : private — user stack (16 × 4 KB = 64 KB),
        ;;                             stack top = 0x40000000
        STACK_VIRT_BASE         equ STACK_VIRT_END - 0x10000            ; 16 × 4 KB
        STACK_VIRT_END          equ USER_STACK_TOP                      ; 0x40000000 (one past last page)
        VDSO_VIRT               equ FUNCTION_TABLE                      ; 0x00010000

pmode_irq0_handler:
        ;; PIT tick.  Increment `system_ticks` (dword in rtc.asm's
        ;; data region, reachable via flat DS), EOI the master PIC,
        ;; iretd.  Interrupt gate entry leaves IF=0 for the body, so
        ;; the `inc dword [mem]` is safe against reentrancy; on a
        ;; single CPU we don't need the LOCK prefix.
        push eax
        inc dword [system_ticks]
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        pop eax
        iretd

pmode_irq6_handler:
        ;; FDC command complete.  EOI.
        push eax
        mov al, PIC_EOI
        out PIC1_CMD_PORT, al
        pop eax
        iretd

;;; -----------------------------------------------------------------------
;;; program_enter
;;;
;;; Builds the per-program PD and `iretd`s into ring 3.  Caller
;;; invariants:
;;;   * Active PD = `kernel_pd_template` (no user mappings).
;;;   * `vfs_find` (or equivalent) has populated `vfs_found_*` for
;;;     the binary file.
;;;   * `buffer_snapshot` (256 B) and `exec_arg_snapshot` (4 B) hold
;;;     the BUFFER / EXEC_ARG content the new program inherits.
;;;
;;; Streams the binary directly from disk into per-program user
;;; frames — sector-by-sector via `vfs_read_sec` and a private
;;; `program_fd` struct — instead of staging through a scratch
;;; buffer.  The trailer (BSS size) is read from the last loaded
;;; user frame after Phase 1, then BSS-only frames are mapped in
;;; Phase 2.
;;;
;;; Never returns.  On panic (allocator OOM or disk error during
;;; PD build) the kernel halts — there's no graceful recovery for
;;; "ran out of frames / lost a sector mid-program-load" yet.
;;; -----------------------------------------------------------------------
program_enter:
        call fd_init

        ;; --- Allocate fresh PD ---
        call address_space_create
        jc .panic
        mov [current_pd_phys], eax

        ;; --- Set up kernel-side fd struct from vfs_found_* ---
        ;; Used by Phase 1's vfs_read_sec calls to walk the binary
        ;; sector-by-sector without going through fd_alloc / the user
        ;; fd table.  Lives in BSS; only one program loads at a time.
        mov edi, program_fd
        xor eax, eax
        mov ecx, FD_ENTRY_SIZE / 4
        cld
        rep stosd
        mov al, [vfs_found_type]
        mov [program_fd + FD_OFFSET_TYPE], al
        mov ax, [vfs_found_inode]
        mov [program_fd + FD_OFFSET_START], ax
        mov eax, [vfs_found_size]
        mov [program_fd + FD_OFFSET_SIZE], eax
        mov ax, [vfs_found_dir_sec]
        mov [program_fd + FD_OFFSET_DIRECTORY_SECTOR], ax
        mov ax, [vfs_found_dir_off]
        mov [program_fd + FD_OFFSET_DIRECTORY_OFFSET], ax

        ;; --- Map shell↔program handoff frame at user-virt USER_DATA_BASE ---
        ;; Holds ARGV (32 B at +0x4DE), EXEC_ARG (4 B at +0x4FC), and
        ;; BUFFER (256 B at +0x500).  The frame sits at user-virt 0x1000
        ;; (PTE[1]) so PTE[0] (virt 0..0xFFF) stays not-present and a NULL
        ;; dereference from CPL=3 raises #PF instead of silently
        ;; reading/writing the handoff frame.  In-frame offsets are
        ;; ``<symbol> - USER_DATA_BASE`` so the per-symbol page offset
        ;; survives any future shift of USER_DATA_BASE.
        call frame_alloc
        jc .panic
        push eax                            ; [esp+0] = handoff frame phys
        mov edi, eax
        add edi, 0xC0000000                 ; kernel-virt of frame
        ;; Zero entire frame.
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi
        ;; EXEC_ARG snapshot.
        mov eax, [exec_arg_snapshot]
        mov [edi + (EXEC_ARG - USER_DATA_BASE)], eax
        ;; BUFFER snapshot.
        push edi
        mov esi, buffer_snapshot
        lea edi, [edi + (BUFFER - USER_DATA_BASE)]
        mov ecx, MAX_INPUT / 4
        rep movsd
        pop edi
        ;; Map the frame at user-virt USER_DATA_BASE.
        pop ecx                             ; handoff frame phys
        mov eax, [current_pd_phys]
        mov ebx, USER_DATA_BASE
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .panic

        ;; --- Phase 1: stream binary pages directly from disk ---
        ;; Each loaded user frame is zero-filled then populated sector-
        ;; by-sector via vfs_read_sec into sector_buffer + a memcpy into
        ;; the frame's direct-map alias.  Last binary frame's phys is
        ;; stashed so the trailer can be peeked after the loop.
        mov dword [last_binary_frame_phys], 0
        mov dword [virt_cursor], PROGRAM_BASE
.phase1_page_loop:
        mov eax, [virt_cursor]
        sub eax, PROGRAM_BASE               ; EAX = file byte offset for this page
        cmp eax, [vfs_found_size]
        jae .phase1_done                    ; past binary end

        call frame_alloc
        jc .panic
        mov [last_binary_frame_phys], eax   ; remember for trailer peek
        push eax                            ; frame phys for map call
        mov edi, eax
        add edi, 0xC0000000                 ; EDI = kernel-virt of frame

        ;; Zero entire frame so the partial last sector lands on a
        ;; zero background.
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi

        ;; Inner loop: 8 sectors per page (or fewer at end of file).
        xor edx, edx                        ; sector_in_page index
.phase1_sector_loop:
        cmp edx, 8
        jae .phase1_page_done

        ;; file_offset = (virt_cursor - PROGRAM_BASE) + sector_in_page * 512
        mov eax, [virt_cursor]
        sub eax, PROGRAM_BASE
        mov ebx, edx
        shl ebx, 9
        add eax, ebx                        ; EAX = file offset for this sector
        cmp eax, [vfs_found_size]
        jae .phase1_page_done               ; past end of binary

        ;; bytes_remaining = binsize - file_offset (bytes still to copy)
        mov ebx, [vfs_found_size]
        sub ebx, eax                        ; EBX = remaining
        cmp ebx, 512
        jbe .phase1_chunk_set
        mov ebx, 512
.phase1_chunk_set:

        mov [program_fd + FD_OFFSET_POSITION], eax

        ;; Read one sector into sector_buffer.
        push ebx
        push edx
        push edi
        mov esi, program_fd
        call vfs_read_sec
        pop edi
        pop edx
        pop ebx
        jc .panic                           ; disk error mid-program-load

        ;; Copy EBX bytes from sector_buffer to (frame + sector_in_page * 512).
        push esi
        push edi
        push edx
        push ecx
        mov esi, sector_buffer
        mov ecx, edx
        shl ecx, 9                          ; ECX = sector_in_page * 512
        add edi, ecx                        ; EDI = frame + offset
        mov ecx, ebx
        cld
        rep movsb
        pop ecx
        pop edx
        pop edi
        pop esi

        inc edx
        jmp .phase1_sector_loop
.phase1_page_done:
        ;; Map the frame into the per-program PD at virt_cursor.
        pop ecx                             ; frame phys
        mov eax, [current_pd_phys]
        mov ebx, [virt_cursor]
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .panic
        add dword [virt_cursor], 0x1000
        jmp .phase1_page_loop
.phase1_done:

        ;; --- Read BSS trailer from the last binary frame ---
        ;; binsize is vfs_found_size; the trailer (6-byte BSS_MAGIC32 or
        ;; legacy 4-byte BSS_MAGIC) sits at offset (binsize - N) within
        ;; the file, which lands inside the last loaded frame at offset
        ;; ((binsize - 1) & 0xFFF) + 1 - N.
        xor ebx, ebx                        ; default bss_size = 0
        mov eax, [last_binary_frame_phys]
        test eax, eax
        jz .have_bss_size                   ; empty file (no binary loaded)
        add eax, 0xC0000000                 ; EAX = kernel-virt of last frame
        mov ecx, [vfs_found_size]
        sub ecx, 1
        and ecx, 0xFFF
        inc ecx                             ; ECX = valid bytes in last frame
        ;; Try 6-byte trailer first (BSS_MAGIC32).
        cmp ecx, 6
        jb .check_old_trailer
        cmp word [eax + ecx - 2], BSS_MAGIC32
        jne .check_old_trailer
        mov ebx, [eax + ecx - 6]
        jmp .have_bss_size
.check_old_trailer:
        cmp ecx, 4
        jb .have_bss_size
        cmp word [eax + ecx - 2], BSS_MAGIC
        jne .have_bss_size
        movzx ebx, word [eax + ecx - 4]
.have_bss_size:

        ;; --- Compute user_image_end ---
        mov eax, [vfs_found_size]
        add eax, ebx                        ; binsize + bsssize
        add eax, PROGRAM_BASE
        add eax, 0xFFF
        and eax, 0xFFFFF000
        mov [user_image_end], eax

        ;; --- Phase 2: BSS-only pages (zero-filled, no disk reads) ---
        ;; virt_cursor was left at page_align_up(PROGRAM_BASE + binsize)
        ;; by Phase 1; loop until user_image_end.
.phase2_page_loop:
        mov eax, [virt_cursor]
        cmp eax, [user_image_end]
        jae .prog_pages_done
        call frame_alloc
        jc .panic
        push eax
        mov edi, eax
        add edi, 0xC0000000
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop ecx
        mov eax, [current_pd_phys]
        mov ebx, [virt_cursor]
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .panic
        add dword [virt_cursor], 0x1000
        jmp .phase2_page_loop
.prog_pages_done:

        ;; --- Map vDSO code page (shared, R-X user) ---
        mov eax, [current_pd_phys]
        mov ebx, VDSO_VIRT
        mov ecx, [vdso_code_phys]
        mov edx, PTE_USER_RX_SHARED
        call address_space_map_page
        jc .panic

        ;; --- Map user stack (private, 16 frames, zeroed) ---
        mov dword [virt_cursor], STACK_VIRT_BASE
.stack_page_loop:
        mov eax, [virt_cursor]
        cmp eax, STACK_VIRT_END
        jae .stack_pages_done
        call frame_alloc
        jc .panic
        push eax
        mov edi, eax
        add edi, 0xC0000000
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop ecx
        mov eax, [current_pd_phys]
        mov ebx, [virt_cursor]
        mov edx, PTE_USER_RW
        call address_space_map_page
        jc .panic
        add dword [virt_cursor], 0x1000
        jmp .stack_page_loop
.stack_pages_done:

        ;; --- Snapshot kernel ESP for sys_exit ---
        mov [shell_esp], esp

        ;; --- Switch to the new PD ---
        mov eax, [current_pd_phys]
        mov cr3, eax

        ;; --- iretd into ring 3 ---
        ;; Reload data segments to USER_DATA_SELECTOR before the iretd
        ;; (iretd doesn't reload DS/ES/FS/GS).  CPL=0 can still
        ;; read/write through those selectors because CPL ≤ DPL on
        ;; access.
        mov ax, USER_DATA_SELECTOR
        mov ds, ax
        mov es, ax
        mov fs, ax
        mov gs, ax
        push dword USER_DATA_SELECTOR
        push dword USER_STACK_TOP
        push dword 0x202
        push dword USER_CODE_SELECTOR
        push dword PROGRAM_BASE
        iretd

.panic:
        ;; Allocator OOM during program load.  No graceful recovery
        ;; yet — halt with '!' on COM1 (matches high_entry's panic).
        mov dx, COM1_DATA
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

protected_mode_entry:
        ;; Segment registers, ESP, GDTR, and IDTR are already in place
        ;; — `high_entry` (kernel.asm) ran first and handed off here
        ;; with the kernel GDT / IDT live and ESP pointing at
        ;; `kernel_stack_top`.  We patch the TSS, ltr, bring up devices,
        ;; allocate the shared vDSO user-page frame, and drop into
        ;; shell_reload.
        ;;
        ;; Patch the TSS descriptor's base bytes with tss_data's linear
        ;; address (the bytes are scattered across descriptor offsets
        ;; +2/+4/+7 so we can't fold them at assemble time without
        ;; line-noise expressions), populate the TSS fields the CPU
        ;; consults on a ring-3 → ring-0 transition (SS0, ESP0), parking
        ;; the I/O permission bitmap past the TSS limit so all I/O ports
        ;; trap from CPL=3.  Then `ltr` — must complete before any ring
        ;; transition can fire, but exceptions and IRQs at CPL=0 don't
        ;; need the TSS, so doing it before the rest of init is safe.
        mov eax, tss_data
        mov [gdt_tss + 2], ax
        shr eax, 16
        mov [gdt_tss + 4], al
        mov [gdt_tss + 7], ah
        mov dword [tss_data + 4], kernel_stack_top      ; ESP0
        mov word [tss_data + 8], 0x10                   ; SS0 = kernel data
        mov word [tss_data + 102], 104                  ; IOPB offset = TSS limit + 1 → no I/O bitmap
        mov ax, TSS_SELECTOR
        ltr ax

        ;; Reprogram PIT to 100 Hz (MS_PER_TICK=10 ms/tick).
        mov al, PIT_MODE2_LOHI_CH0
        out PIT_COMMAND, al
        mov al, PIT_DIVISOR & 0xFF
        out PIT_CHANNEL0, al
        mov al, PIT_DIVISOR >> 8
        out PIT_CHANNEL0, al

        ;; Install 32-bit IRQ handlers.
        mov eax, pmode_irq0_handler
        mov bl, PMODE_IRQ0_VECTOR
        call idt_set_gate32
        mov eax, pmode_irq6_handler
        mov bl, PMODE_IRQ6_VECTOR
        call idt_set_gate32

        ;; Zero the system tick counter and unmask IRQ 0 (PIT) before
        ;; the driver inits run, so any timing primitive that runs
        ;; during init (e.g. fdc_motor_start's rtc_sleep_ms during
        ;; vfs_init's first read on a floppy boot) sees ticks
        ;; advancing.
        mov dword [system_ticks], 0
        in al, PIC1_DATA_PORT
        and al, 0FEh                    ; clear bit 0 (unmask IRQ 0)
        out PIC1_DATA_PORT, al
        sti

        ;; Allocate shared user-page frames for the vDSO code page.
        ;; Mapped (with PTE_SHARED) into every per-program PD by
        ;; program_enter; address_space_destroy skips it on teardown.
        call vdso_install

        call ata_init
        call fd_init
        call fdc_init
        call ps2_init
        call vfs_init
        ;; Probe the NE2000 NIC and bring it up if present.  CF set =
        ;; no NIC, which is fine — netinit / net programs surface that
        ;; via a "no NIC" message rather than halting the kernel.
        call network_initialize

        call vga_clear_screen

        ;; Print welcome banner to COM1 and VGA.
        mov esi, welcome_msg
        .banner:
        mov al, [esi]
        test al, al
        jz .banner_done
        call put_character
        inc esi
        jmp .banner
        .banner_done:
        ;; Fall through into shell_reload.

shell_reload:
        ;; Active PD: kernel_pd_template (sys_exit just destroyed the
        ;; dying program's PD and switched off it, or this is the first
        ;; boot and CR3 was set up by high_entry).  Look up bin/shell
        ;; (program_enter streams its bytes from disk on demand), reset
        ;; the BUFFER / EXEC_ARG snapshots, and jmp program_enter.
        mov esi, shell_path
        call vfs_find
        jc .shell_fail
        ;; Fresh shell inherits no args.  Zero both snapshots.
        mov edi, buffer_snapshot
        mov ecx, MAX_INPUT / 4
        xor eax, eax
        cld
        rep stosd
        mov dword [exec_arg_snapshot], 0
        jmp program_enter

        .shell_fail:
        ;; Missing or unreadable shell.  Halt — no recovery here.
        cli
        hlt
        jmp $-1

vdso_install:
        ;; Allocate one frame for the vDSO code page and copy
        ;; `vdso_image` (the embedded blob from kernel.asm) into it via
        ;; the kernel direct map.  program_enter installs the frame at
        ;; user-virt FUNCTION_TABLE (0x10000) in every per-program PD
        ;; with PTE_SHARED, so user programs see the vDSO and
        ;; address_space_destroy never frees the frame.
        push eax
        push ecx
        push esi
        push edi
        call frame_alloc
        jc .panic
        mov [vdso_code_phys], eax
        mov edi, eax
        add edi, 0xC0000000             ; direct-map kernel-virt of frame
        mov esi, vdso_image
        mov ecx, (vdso_image_end - vdso_image) / 4
        cld
        rep movsd
        pop edi
        pop esi
        pop ecx
        pop eax
        ret
.panic:
        mov dx, COM1_DATA
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

        ;; Physical address of `kernel_pd_template`, the page directory
        ;; whose top-256 PDEs are copied into every per-program PD as
        ;; the kernel half of the address space.  `high_entry` records
        ;; the boot PD's phys here; `address_space_create` reads it
        ;; when seeding each new per-program PD's kernel half.
kernel_pd_template_phys dd 0

        ;; Per-program-load state used by program_enter.
current_pd_phys         dd 0    ; new PD being built
last_binary_frame_phys  dd 0    ; phys of the last loaded binary frame (for trailer peek)
user_image_end          dd 0    ; PROGRAM_BASE + binsize + bsssize, page-aligned up
virt_cursor             dd 0    ; current user-virt during page-walk loops
vdso_code_phys          dd 0    ; phys of the shared vDSO code frame

        ;; Kernel-side fd struct used by program_enter to stream the
        ;; program binary directly from disk into per-program user
        ;; frames (sector-by-sector via vfs_read_sec).  Sized to
        ;; FD_ENTRY_SIZE so the FD_OFFSET_* layout matches the user fd
        ;; table, even though this slot lives outside it.  Only one
        ;; program loads at a time, so a single static slot suffices.
        align 4
program_fd              times FD_ENTRY_SIZE db 0

shell_esp       dd 0            ; kernel ESP snapshot, restored by sys_exit
shell_path      db "bin/shell", 0

        ;; BUFFER / EXEC_ARG cross-AS snapshot.  sys_exec writes these
        ;; from the dying shell's user pages BEFORE address_space_destroy;
        ;; program_enter copies them into the new program's first user
        ;; frame at user-virt 0x500 / 0x4FC.  shell_reload zeroes them
        ;; (a fresh shell inherits no args).
        align 4
buffer_snapshot         times MAX_INPUT db 0
exec_arg_snapshot       dd 0

        ;; 32-bit TSS.  Only SS0/ESP0/IOPB-offset are populated (in
        ;; protected_mode_entry); all other fields stay zero because we
        ;; don't use hardware task switching.  Sized to the 104-byte
        ;; standard layout so the IOPB-past-limit trick parks I/O.
        align 4
tss_data:
        times 104 db 0

welcome_msg     db "Welcome to BBoeOS!", 13, 10, "Version 0.8.1 (2026/04/28)", 13, 10, 0

;;; -----------------------------------------------------------------------
