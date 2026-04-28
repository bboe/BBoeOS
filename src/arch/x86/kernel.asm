;;; ------------------------------------------------------------------------
;;; kernel.asm — high-half kernel binary (org 0xC0100000).
;;;
;;; Loaded onto disk after boot.bin and read into physical 0x10000 by
;;; boot.asm's real-mode INT 13h, then relocated to physical 0x100000
;;; (= virtual 0xC0100000) by `early_pe_entry` once paging is on.
;;;
;;; The very first byte of kernel.bin is `high_entry`, which boot.asm's
;;; far-jump targets at virtual address HIGH_ENTRY_VIRT (0xC0100000).
;;; high_entry installs the kernel GDT/IDT/stack, drops the boot
;;; identity mapping, initializes the bitmap frame allocator, allocates
;;; the remaining 63 kernel direct-map PTs that fan out the kernel
;;; direct map (PDEs 769..831, virt 0xC0400000..0xCFFFFFFF), and jumps
;;; into `protected_mode_entry` for driver / VFS / NIC / shell bring-up.
;;;
;;; Phase 4: each program runs in its own per-program PD built by
;;; `address_space_create` from `program_enter` (entry.asm).  The PD's
;;; kernel half (PDEs 768..1023) is copy-imaged from
;;; `kernel_pd_template` so the kernel direct map is reachable from
;;; every address space.  The user half (PDEs 0..767) starts empty and
;;; is populated only with the program's own pages, plus shared
;;; vDSO/JUMP_TABLE PTEs marked with the AVL[0] PTE_SHARED bit so
;;; `address_space_destroy` skips frame_free on them.  Programs run
;;; with PROGRAM_BASE=0x08048000 and USER_STACK_TOP=0x40000000 (Linux
;;; ELF convention); BUFFER (0x500), EXEC_ARG (0x4FC), and the vDSO
;;; (0x10000) stay at low user-virt and reach the program through the
;;; per-program PD's first PT.
;;; ------------------------------------------------------------------------

        org 0C0100000h
        bits 32
        %include "constants.asm"

        ;; The kernel reads boot-time state (boot_disk, directory_sector)
        ;; via the direct map at fixed low-physical addresses set by
        ;; boot.asm before paging.  EQU aliases let kernel includes
        ;; (drivers/fs.asm, fs/bbfs.asm, …) keep using the natural
        ;; `[boot_disk]` / `[directory_sector]` syntax.
boot_disk        equ BOOT_DISK_VIRT
directory_sector equ DIRECTORY_SECTOR_VIRT

        ;; Disk sector buffer at fixed low-phys 0xF000 (kernel-virt
        ;; 0xC000F000 via the direct map).  Stays at fixed phys for now;
        ;; PR A migrated bbfs.asm / ext2.asm to 32-bit register accesses
        ;; so it'll keep working post-shim via the kernel direct map.
sector_buffer    equ 0xC000F000

        BOOT_PD_PHYS            equ 0x200000
        ;; Kernel-virt base of the direct map.  Subtract from any
        ;; kernel-virt address to recover its physical alias.
        DIRECT_MAP_BASE         equ 0C0000000h
        FIRST_KERNEL_PT_PHYS    equ 0x201000
        KERNEL_FINAL_PHYS       equ 0x100000
        LOW_RESERVE_BYTES       equ FIRST_KERNEL_PT_PHYS + 0x1000  ; 0x202000 — bitmap-allocator sweep ceiling
        ;; Ring-0 stack: 16 KB at phys 0x180000, accessed through the
        ;; direct map at virt 0xC0180000.  Lives outside kernel.bin to
        ;; avoid burning 16 KB of zero padding on disk; the bitmap
        ;; allocator reserves the underlying frames at boot via the
        ;; LOW_RESERVE_BYTES sweep above (the sweep covers 0..0x202000,
        ;; which naturally includes the stack).  Reachable from the
        ;; very first instructions in `high_entry` because early-PE's
        ;; PDE[768] direct map already covers phys 0..0x3FFFFF.  Sits
        ;; above the kernel image (0x100000+) and below the 4 MB
        ;; early-PE direct-map ceiling, outside the user shim's
        ;; user-accessible windows.
        KERNEL_STACK_BYTES      equ 0x4000                       ; 16 KB
        KERNEL_STACK_PHYS       equ 0x180000
        KERNEL_STACK_TOP_PHYS   equ KERNEL_STACK_PHYS + KERNEL_STACK_BYTES
        kernel_stack            equ DIRECT_MAP_BASE + KERNEL_STACK_PHYS
        kernel_stack_top        equ DIRECT_MAP_BASE + KERNEL_STACK_TOP_PHYS
        ;; NE2000 polled-mode TX/RX scratch — same trick as the
        ;; kernel stack: backing frames at fixed phys (right after
        ;; the stack) reached through the direct map, so the buffers
        ;; don't burn 3 KB of zero padding inside kernel.bin.  1536
        ;; bytes apiece — one max-size Ethernet frame each (1500 MTU
        ;; + 14-byte header + slop).  LOW_RESERVE_BYTES above covers
        ;; the entire region.
        NET_BUFFER_BYTES        equ 1536
        NET_RECEIVE_BUFFER_PHYS equ KERNEL_STACK_TOP_PHYS               ; 0x184000
        NET_TRANSMIT_BUFFER_PHYS equ NET_RECEIVE_BUFFER_PHYS + NET_BUFFER_BYTES
        net_receive_buffer      equ DIRECT_MAP_BASE + NET_RECEIVE_BUFFER_PHYS
        net_transmit_buffer     equ DIRECT_MAP_BASE + NET_TRANSMIT_BUFFER_PHYS
        ;; Program-load scratch buffer.  vfs_load writes the freshly-
        ;; loaded binary here; program_enter copies from here into the
        ;; per-program PD's user pages.  128 KB headroom comfortably
        ;; covers every program in src/c/ (largest is ~22 KB today).
        ;; Lives at fixed phys above the NE2000 buffers, reached via
        ;; the kernel direct map; same trick as kernel_stack — keeps
        ;; the bytes out of kernel.bin's on-disk image.
        ;; LOW_RESERVE_BYTES (0x202000) covers the whole range.
        PROGRAM_SCRATCH_BYTES   equ 128 * 1024                          ; 128 KB
        PROGRAM_SCRATCH_PHYS    equ 0x185000                            ; aligned, just past NIC buffers
        program_scratch         equ DIRECT_MAP_BASE + PROGRAM_SCRATCH_PHYS
        E820_TABLE_VIRT         equ DIRECT_MAP_BASE + 0x500
        FIRST_KERNEL_PDE        equ 768
        LAST_KERNEL_PDE         equ 832         ; PDEs [768..831]: 64 entries × 4 MB = 256 MB
        KERNEL_CODE_SELECTOR    equ 08h
        KERNEL_DATA_SELECTOR    equ 10h

high_entry:
        ;; --- Switch onto kernel-virt addresses for stack/GDT/IDT ---
        ;;
        ;; CS was loaded by boot's far-jump (0x08) and the GDT cache
        ;; for DS/ES/SS/FS/GS still holds the data-selector descriptor
        ;; from early-PE.  We now retarget GDTR onto the kernel's own
        ;; GDT (a kernel-virt copy below) and reload every segment
        ;; register so subsequent segment loads — including the
        ;; CPU-driven loads on interrupts — find the GDT through the
        ;; direct map rather than at low physical (which is about to
        ;; disappear).
        lgdt [kernel_gdtr]

        ;; Reload data segments via the kernel data selector.  The
        ;; descriptor is identical in both GDTs, so the cached values
        ;; would still work; reloading just refreshes them through the
        ;; new GDTR for clarity.
        mov ax, KERNEL_DATA_SELECTOR
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov fs, ax
        mov gs, ax

        ;; Reload CS via a far-jump through the new GDT.
        jmp KERNEL_CODE_SELECTOR:.cs_reloaded
.cs_reloaded:

        ;; Switch ESP to the kernel stack (16 KB at virt 0xC0180000,
        ;; backed by phys 0x180000+; see KERNEL_STACK_PHYS for why it
        ;; lives here instead of inside kernel.bin).  Reachable
        ;; immediately because PDE[768]'s direct map covers
        ;; phys 0..0x3FFFFF.  TSS.ESP0 is patched to the same later
        ;; in protected_mode_entry.
        mov esp, kernel_stack_top

        ;; Patch the high-half offsets of the static IDT entries (the
        ;; macros only emit the low 16 bits — see idt.asm for why),
        ;; then install the kernel IDT.  An exception or interrupt
        ;; fired before this point would triple-fault; from here on,
        ;; vectors route through `exc_common` in idt.asm.
        call idt_init
        lidt [idtr]

        ;; --- Drop the identity mapping at PDE[0] ---
        ;;
        ;; Boot's PD lives at physical BOOT_PD_PHYS (0x1000), which is
        ;; reachable via the kernel direct map at virt
        ;; DIRECT_MAP_BASE + 0x1000 = 0xC0001000.  Zero the PDE that
        ;; identity-maps virt 0..0x3FFFFF, then full TLB flush via CR3
        ;; reload.  Boot.asm's GDT and code at low physical are now
        ;; permanently unreachable; we already re-lgdt'd onto the
        ;; kernel GDT so segment loads find the kernel GDT through the
        ;; direct map.
        mov dword [DIRECT_MAP_BASE + BOOT_PD_PHYS + 0*4], 0
        mov eax, cr3
        mov cr3, eax

        ;; Promote the boot PD into kernel_pd_template by recording its
        ;; physical address.  Future per-program PDs (Phase 4) will
        ;; copy its top-256 PDEs as their kernel-half mapping.
        mov dword [kernel_pd_template_phys], BOOT_PD_PHYS

        ;; --- Initialize the bitmap frame allocator from E820 ---
        ;;
        ;; The probe ran in real mode and stashed entries at physical
        ;; 0x500; the direct map exposes the same bytes at virt
        ;; E820_TABLE_VIRT.
        mov esi, E820_TABLE_VIRT
        call frame_init

        ;; Reserve everything from phys 0 up to and including the
        ;; boot PD and first kernel PT in one sweep.  Covers BIOS /
        ;; VGA / staging region / kernel image / kernel stack (at
        ;; phys 0x180000) / NE2000 RX/TX scratch (0x184000+) /
        ;; program_scratch (0x185000+) / boot PD / first kernel PT.
        ;; The bitmap allocator only ever returns frames at phys
        ;; LOW_RESERVE_BYTES (0x202000) and above, so the kernel PTs
        ;; allocated next, every PD/PT/page built by
        ;; `address_space_create` / `address_space_map_page`, and the
        ;; vDSO + JUMP_TABLE shared frames all land in the
        ;; high-physical region above LOW_RESERVE_BYTES.
        xor eax, eax
        mov ecx, LOW_RESERVE_BYTES
        call frame_reserve_range

        ;; --- Allocate the remaining 63 kernel PTs to fill out the
        ;; 256 MB direct map at 0xC0000000..0xCFFFFFFF ---
        ;;
        ;; Each new PT covers 4 MB; install at PDE[FIRST_KERNEL_PDE+1]
        ;; through PDE[LAST_KERNEL_PDE-1].  The bitmap's first-fit
        ;; allocations land in the still-mapped first 4 MB (frames
        ;; below 0x400000 that aren't already reserved), so each new
        ;; PT is reachable via the existing PDE[768] direct map for
        ;; population — no kmap slot needed.
        mov ebx, FIRST_KERNEL_PDE + 1           ; first new PDE index
.alloc_kernel_pt:
        cmp ebx, LAST_KERNEL_PDE
        jae .alloc_done
        call frame_alloc
        jc .panic                               ; OOM at boot — fatal
        push eax                                ; save phys for the PDE install
        mov edi, eax
        add edi, DIRECT_MAP_BASE                ; kernel-virt to populate the new PT

        ;; Populate PT entries.  This PT's PDE[ebx] covers virt
        ;; (ebx * 4 MB)..(ebx * 4 MB + 4 MB - 1).  Subtract
        ;; FIRST_KERNEL_PDE * 4 MB to get the physical base.  Each
        ;; PTE[j] = (chunk_base + j * 4 KB) | P | RW | G.
        mov eax, ebx
        sub eax, FIRST_KERNEL_PDE
        shl eax, 22                             ; chunk_base = (ebx-768)*4 MB
        xor ecx, ecx
.pt_fill:
        mov edx, eax
        or edx, 0x103                           ; P | RW | G
        mov [edi + ecx*4], edx
        add eax, 0x1000
        inc ecx
        cmp ecx, 1024
        jb .pt_fill

        ;; Install the new PT at PDE[ebx] in kernel_pd_template (which
        ;; lives at BOOT_PD_PHYS, reached through the direct map).
        pop eax
        or eax, 0x003                           ; P | RW (kernel-only)
        mov edi, DIRECT_MAP_BASE + BOOT_PD_PHYS
        mov [edi + ebx*4], eax

        inc ebx
        jmp .alloc_kernel_pt
.alloc_done:

        ;; Flush TLB after the PD changes.
        mov eax, cr3
        mov cr3, eax

        ;; Continue with the existing post-flip init: TSS / IDT IRQ
        ;; gates / drivers / VFS / NIC / banner / shell.  Lives in
        ;; entry.asm's `protected_mode_entry`, trimmed to skip the
        ;; segment / ESP / lidt work `high_entry` already performed.
        ;;
        ;; Phase 4 PR C drops the temporary user shim that lived at
        ;; PDE[0] of kernel_pd_template — programs now run in private
        ;; per-program PDs built by `address_space_create` from
        ;; `program_enter`.  kernel_pd_template's user half is
        ;; entirely zero-filled, so kernel-mode code running on it
        ;; cannot accidentally touch user memory.
        jmp protected_mode_entry

.panic:
        ;; Allocator OOM during boot — print '!' on COM1 and halt.
        ;; serial.kasm emits COM1_DATA as a `%define` (cc.py's preprocessor
        ;; output) that NASM only sees after the `%include` below — too
        ;; late for this prologue.  Literal port works everywhere.
        mov dx, 0x3F8
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

%include "memory_management/address_space.asm"
%include "memory_management/frame.asm"
%include "drivers/ata.asm"
%include "drivers/console.asm"
%include "drivers/fdc.asm"
%include "drivers/ne2k.asm"
%include "drivers/ps2.kasm"
%include "drivers/rtc.asm"
%include "drivers/serial.kasm"
%include "drivers/vga.asm"
%include "entry.asm"
%include "fs/block.asm"
%include "fs/fd.kasm"
%include "fs/vfs.asm"
%include "net/net.asm"
%include "syscall.asm"
%include "idt.asm"
%include "system.kasm"

;;; ----- Kernel GDT (kernel-virt copy of the boot GDT) -----
        align 8
kernel_gdt_start:
        dq 0                            ; 0x00 null

        ;; 0x08 kernel code: base=0, limit=4 GB, 32-bit, DPL=0
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 10011010b
        db 11001111b
        db 0x00

        ;; 0x10 kernel data: base=0, limit=4 GB, 32-bit, DPL=0
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 10010010b
        db 11001111b
        db 0x00

        ;; 0x18 user code: base=0, limit=4 GB, 32-bit, DPL=3
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 11111010b
        db 11001111b
        db 0x00

        ;; 0x20 user data: base=0, limit=4 GB, 32-bit, DPL=3
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 11110010b
        db 11001111b
        db 0x00

        ;; 0x28 TSS descriptor.  Base bytes patched at runtime by
        ;; protected_mode_entry — `tss_data` is a kernel BSS label and
        ;; the descriptor encoding scatters base across non-contiguous
        ;; bytes, so a static encoding here would force NASM to fold a
        ;; forward reference through `& 0xFFFF` / `>> 16` arithmetic.
gdt_tss:
        dw 103
        dw 0x0000               ; base[15:0]  — patched
        db 0x00                 ; base[23:16] — patched
        db 10001001b
        db 00000000b
        db 0x00                 ; base[31:24] — patched
kernel_gdt_end:

kernel_gdtr:
        dw kernel_gdt_end - kernel_gdt_start - 1
        dd kernel_gdt_start

        ;; vDSO image — separately-assembled blob copied to virtual
        ;; FUNCTION_TABLE (0x00010000) at boot by `vdso_install` in
        ;; entry.asm.  Holds the FUNCTION_TABLE jump block plus the
        ;; shared_* helper bodies; user programs call into it via the
        ;; FUNCTION_* constants in constants.asm.  Phase 3 reaches it
        ;; through the user shim that maps virt 0..0xFFFFF.
        align 4
vdso_image:
        incbin "vdso.bin"
vdso_image_end:

kernel_end:
