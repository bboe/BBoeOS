;;; ------------------------------------------------------------------------
;;; kernel.asm — high-half kernel binary (org 0xC0020000).
;;;
;;; Loaded onto disk after boot.bin and read into physical 0x20000 by
;;; boot.asm's real-mode INT 13h.  The phys load address sits in
;;; conventional RAM (above the vDSO target at 0x10000, below the VGA
;;; aperture at 0xA0000) so the entire kernel-side reserved region
;;; fits under 1 MB and the OS can boot under QEMU `-m 1`.  The
;;; kernel `org` is KERNEL_VIRT_BASE + KERNEL_LOAD_PHYS, which means
;;; the kernel runs at its direct-map alias — no separate
;;; higher-half PT.
;;;
;;; The very first byte of kernel.bin is `high_entry`, which boot.asm's
;;; far-jump targets at virtual address HIGH_ENTRY_VIRT
;;; (KERNEL_VIRT_BASE + KERNEL_LOAD_PHYS = 0xFF820000).  high_entry
;;; installs the kernel GDT/IDT/stack, drops the boot identity
;;; mapping, initializes the bitmap frame allocator, allocates the
;;; kernel direct-map PTs needed for installed RAM (a no-op when
;;; FIRST_KERNEL_PDE = 1022 — the first kernel PT covering phys
;;; 0..4 MB suffices), brings up the kmap window via `kmap_init`,
;;; and jumps into `protected_mode_entry` for driver / VFS / NIC /
;;; shell bring-up.
;;;
;;; Each program runs in its own per-program PD built by
;;; `address_space_create` from `program_enter` (entry.asm).  The PD's
;;; kernel half (PDEs FIRST_KERNEL_PDE..1023) is copy-imaged from
;;; `kernel_idle_pd` so the kernel direct map is reachable from
;;; every address space.  The user half (PDEs 0..767) starts empty and
;;; is populated only with the program's own pages, plus the shared
;;; vDSO PTE marked with the AVL[0] PTE_SHARED bit so
;;; `address_space_destroy` skips frame_free on it.  Programs run
;;; with PROGRAM_BASE=0x08048000 and USER_STACK_TOP=0x40000000 (Linux
;;; ELF convention); BUFFER (0x500), EXEC_ARG (0x4FC), and the vDSO
;;; (0x10000) stay at low user-virt and reach the program through the
;;; per-program PD's first PT.
;;; ------------------------------------------------------------------------

        org 0FF820000h
        bits 32
        %include "constants.asm"

        ;; Trampoline + boot stash at the very top of kernel.bin.
        ;; boot.asm's far-jump targets virt 0xC0020000 = the first byte
        ;; of kernel.bin; the trampoline skips past the stash to
        ;; high_entry.  boot.asm writes boot_disk and directory_sector
        ;; here AFTER loading kernel.bin (so the writes don't get
        ;; overwritten by the load), then the kernel reads them via
        ;; PDE[768]'s direct map.  Embedding them inside kernel.bin lets
        ;; us drop the legacy phys 0x4D0 / 0x4D2 reservation: the IVT /
        ;; BDA / 0x600-0x7BFF gap / MBR landing zone all stay in the
        ;; bitmap allocator's free pool.
        jmp short high_entry            ; 2 bytes (offset 0)
boot_disk        db 0                   ; offset 2  (BOOT_STASH_OFFSET)
directory_sector dw 0                   ; offset 3
        ;; Pad to align high_entry on a 4-byte boundary.
        times 8 - ($ - $$) db 0

        ;; Kernel-side memory layout.  In-memory order (low to high):
        ;;
        ;;   E820 table at phys 0x500             (read-only, from boot.asm)
        ;;   vDSO target at phys 0x10000          (1 page, mapped per-PD)
        ;;   kernel.bin at KERNEL_LOAD_PHYS       (image; var size)
        ;;   KERNEL_RESERVED_BASE                 (page-aligned post-image)
        ;;     kernel_stack                       (KERNEL_STACK_BYTES = 4 KB)
        ;;   BOOT_PD_PHYS                         (4 KB)
        ;;   FIRST_KERNEL_PT_PHYS                 (4 KB)
        ;;   FRAME_BITMAP_PHYS                    (frame_bitmap_bytes — runtime, ≤ 32 KB at the 1 GB direct-map cap)
        ;;   end-of-reserved (= FRAME_BITMAP_PHYS + frame_bitmap_bytes)
        ;;
        ;; KERNEL_RESERVED_BASE is the first page above kernel.bin,
        ;; computed by make_os.sh and passed as -DKERNEL_RESERVED_BASE=N.
        ;; The fallback below keeps direct nasm invocations working with
        ;; a valid (if not maximally packed) layout.
        ;;
        ;; The post-kernel cluster (stack / boot PD / first PT /
        ;; frame_bitmap) lives outside kernel.bin so the on-disk image
        ;; doesn't carry their zero-initialized bytes.  The kernel's
        ;; reserve sweep covers [KERNEL_LOAD_PHYS, FRAME_BITMAP_PHYS +
        ;; frame_bitmap_bytes); the bitmap's byte length is sized at
        ;; boot from the highest type=1 E820 base (clamped to the
        ;; direct-map ceiling — see LAST_KERNEL_PDE), so a `-m 1`
        ;; session pays only ~20 bytes for the bitmap while a
        ;; `-m 1024` session pays 32 KB.
        ;;
        ;; The legacy program_scratch staging buffer (32 KB) is gone:
        ;; program_enter streams the binary directly from disk into
        ;; per-program user frames via vfs_read_sec, sector by sector.
        ;;
        ;; sector_buffer (512 B) and ext2_sd_buffer (1 KB) share a
        ;; single 4 KB FS scratch frame allocated by `vfs_init` from
        ;; the bitmap allocator on every boot (FS is always used).
        ;; sector_buffer is now a `uint8_t *` pointer in src/fs/vfs.c
        ;; BSS — bbfs.asm / ext2.asm callers indirect through
        ;; `[sector_buffer]` to load the base, then `[reg + offset]`.
        ;; ext2_sd_buffer is the runtime pointer at sector_buffer+512,
        ;; populated by ext2_init only on a successful ext2 detect.
        ;;
        ;; net_receive_buffer / net_transmit_buffer / arp_table /
        ;; udp_buffer share one 4 KB NIC scratch frame allocated by
        ;; `network_initialize` only when the NIC is detected;
        ;; sessions booted without a NIC never spend the frame.
        %ifndef KERNEL_RESERVED_BASE
        %define KERNEL_RESERVED_BASE 0x40000
        %endif
        BOOT_PD_PHYS             equ KERNEL_STACK_TOP_PHYS
        DIRECT_MAP_BASE          equ 0FF800000h          ; equals KERNEL_VIRT_BASE; the user/kernel split lives here
        E820_TABLE_VIRT          equ DIRECT_MAP_BASE + 0x500
        FIRST_KERNEL_PDE         equ 1022                ; KERNEL_VIRT_BASE / 0x400000; one PDE of direct map + the kmap window at PDE 1023
        FIRST_KERNEL_PT_PHYS     equ BOOT_PD_PHYS + 0x1000
        FRAME_BITMAP_PHYS        equ FIRST_KERNEL_PT_PHYS + 0x1000
        ;; Direct-map ceiling: kernel-virt addresses below
        ;; DIRECT_MAP_BASE + FRAME_DIRECT_MAP_LIMIT alias the
        ;; corresponding low-physical frames 1:1 via PDEs
        ;; FIRST_KERNEL_PDE..LAST_KERNEL_PDE-1 (PDE 1023 belongs to
        ;; the kmap window).  At FIRST_KERNEL_PDE = 1022 the direct
        ;; map covers exactly 4 MB — sufficient for the resident
        ;; kernel image (~29 KB) plus boot reserved cluster (≤140 KB)
        ;; with massive headroom.  Frames at higher physical
        ;; addresses need a kmap_map slot — see
        ;; memory_management/kmap.asm.
        FRAME_DIRECT_MAP_LIMIT   equ (LAST_KERNEL_PDE - FIRST_KERNEL_PDE) * 0x400000
        ;; Bitmap clamp.  RAM above this is silently ignored — the
        ;; allocator can describe at most ~4 GB minus one page (the
        ;; 32-bit physical address space ceiling).  At -m 4096 the
        ;; bitmap costs ~128 KB; sessions with smaller -m pay less
        ;; (frame_init sizes the bitmap from the highest E820 base).
        FRAME_PHYSICAL_LIMIT     equ 0xFFFFF000
        KERNEL_CODE_SELECTOR     equ 08h
        KERNEL_DATA_SELECTOR     equ 10h
        KERNEL_LOAD_PHYS         equ 0x20000
        KERNEL_STACK_BYTES       equ 0x1000                              ; 4 KB (peak measured ~412 B; ~10× margin)
        KERNEL_STACK_PHYS        equ KERNEL_RESERVED_BASE
        KERNEL_STACK_TOP_PHYS    equ KERNEL_STACK_PHYS + KERNEL_STACK_BYTES
        LAST_KERNEL_PDE          equ 1023        ; PDEs [768..1022]: 255 entries × 4 MB = ~1020 MB direct map; PDE 1023 reserved for the kmap window
        frame_bitmap             equ DIRECT_MAP_BASE + FRAME_BITMAP_PHYS
        kernel_stack             equ DIRECT_MAP_BASE + KERNEL_STACK_PHYS
        kernel_stack_top         equ DIRECT_MAP_BASE + KERNEL_STACK_TOP_PHYS

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

        ;; Switch ESP to the kernel stack (4 KB at KERNEL_RESERVED_BASE,
        ;; reached through the direct map at kernel-virt
        ;; DIRECT_MAP_BASE + KERNEL_RESERVED_BASE; see KERNEL_STACK_PHYS
        ;; for why it lives here instead of inside kernel.bin).  Reachable
        ;; immediately because PDE[768]'s direct map covers phys
        ;; 0..0x3FFFFF.  TSS.ESP0 is patched to the same later in
        ;; protected_mode_entry.
        mov esp, kernel_stack_top

        ;; Poison-fill the kernel stack with 0xDEADBEEF dwords.  Used
        ;; as a canary for future stack-depth instrumentation: a debug
        ;; routine can scan kernel_stack upward for the first
        ;; non-poisoned dword to find the high-water mark (since stack
        ;; values, once written, are never re-poisoned).  The 4 KB
        ;; stack is sized at ~10× the measured peak (~412 B); the
        ;; canary is also a tripwire if a future regression eats deep
        ;; into the stack.  high_entry has nothing to preserve in
        ;; EAX/ECX/EDI yet (boot's pre-paging code didn't pass
        ;; anything through), so a one-shot rep stosd is fine.  Runs
        ;; before lidt so any exception here triple-faults (which is
        ;; what would happen without the fill).
        mov edi, kernel_stack
        mov ecx, KERNEL_STACK_BYTES / 4
        mov eax, 0xDEADBEEF
        cld
        rep stosd

        ;; Patch the high-half offsets of the static IDT entries (the
        ;; macros only emit the low 16 bits — see idt.asm for why),
        ;; then install the kernel IDT.  An exception or interrupt
        ;; fired before this point would triple-fault; from here on,
        ;; vectors route through `exc_common` in idt.asm.
        call idt_init
        lidt [idtr]

        ;; --- Drop the identity mapping at PDE[0] ---
        ;;
        ;; Boot's PD lives at physical BOOT_PD_PHYS (derived from
        ;; KERNEL_RESERVED_BASE by make_os.sh), reachable via the kernel
        ;; direct map at virt DIRECT_MAP_BASE + BOOT_PD_PHYS.  Zero the PDE
        ;; that identity-maps virt 0..0x3FFFFF, then full TLB flush via CR3
        ;; reload.  Boot.asm's GDT and code at low physical are now
        ;; permanently unreachable; we already re-lgdt'd onto the kernel GDT.
        mov dword [DIRECT_MAP_BASE + BOOT_PD_PHYS + 0*4], 0
        mov eax, cr3
        mov cr3, eax

        ;; --- Initialize the bitmap frame allocator from E820 ---
        ;;
        ;; The probe ran in real mode and stashed entries at physical
        ;; 0x500; the direct map exposes the same bytes at virt
        ;; E820_TABLE_VIRT.
        mov esi, E820_TABLE_VIRT
        call frame_init

        ;; Reserve only the regions the kernel still owns post-boot.
        ;; The IVT / BDA / E820-staging page / 0x600..0x7BFF gap /
        ;; MBR + post-MBR boot code / FD-table page / boot stack are
        ;; all dead by now and stay free in the bitmap so the user
        ;; pool can grow into them.  Two narrow reserves:
        ;;
        ;;   1. vDSO target frame at phys 0x10000.  One 4 KB page.
        ;;      The vDSO is mapped into every per-program PD as a
        ;;      shared user code page, so its phys location must
        ;;      stay pinned.
        ;;   2. Kernel image and KERNEL_RESERVED_BASE region:
        ;;      KERNEL_LOAD_PHYS..(FRAME_BITMAP_PHYS + frame_bitmap_bytes).
        ;;      Covers the kernel image, kernel stack, boot PD, first
        ;;      kernel PT, and the runtime-sized frame_bitmap.
        mov eax, 0x10000
        mov ecx, 0x1000                 ; vDSO target page
        call frame_reserve_range
        mov eax, KERNEL_LOAD_PHYS
        mov ecx, [frame_bitmap_bytes]
        add ecx, FRAME_BITMAP_PHYS - KERNEL_LOAD_PHYS
        call frame_reserve_range

        ;; --- Allocate kernel PTs for installed RAM only ---
        ;;
        ;; Each new PT covers 4 MB; install at PDE[FIRST_KERNEL_PDE+1]
        ;; through PDE[dynamic_limit-1].  The initial PDE[768] PT already
        ;; covers phys 0..4 MB, so we only need extra PTs for RAM above
        ;; that.  frame_max_phys (set by frame_init) is the highest free
        ;; frame's base; shr by 22 gives its 4 MB chunk index.
        ;; ESI = (frame_max_phys >> 22) + FIRST_KERNEL_PDE + 1, capped at
        ;; LAST_KERNEL_PDE.  On a 4 MB system this equals FIRST_KERNEL_PDE+1
        ;; and the loop body never executes.
        mov esi, [frame_max_phys]
        shr esi, 22
        add esi, FIRST_KERNEL_PDE + 1
        cmp esi, LAST_KERNEL_PDE
        jbe .cap_ok
        mov esi, LAST_KERNEL_PDE
.cap_ok:
        mov ebx, FIRST_KERNEL_PDE + 1           ; first new PDE index
.alloc_kernel_pt:
        cmp ebx, esi
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

        ;; Install the new PT at PDE[ebx] in the boot PD (which
        ;; lives at BOOT_PD_PHYS, reached through the direct map).
        ;; This kernel-half PDE block gets copy-imaged into
        ;; `kernel_idle_pd` after the loop and inherited by every
        ;; subsequent per-program PD via `address_space_create`.
        pop eax
        or eax, 0x003                           ; P | RW (kernel-only)
        mov edi, DIRECT_MAP_BASE + BOOT_PD_PHYS
        mov [edi + ebx*4], eax

        inc ebx
        jmp .alloc_kernel_pt
.alloc_done:

        ;; --- Allocate the kernel idle PD; free the boot PD ---
        ;;
        ;; The boot PD now holds the final kernel-half mapping: PDE[768]
        ;; (the 4 MB direct map for phys 0..0x3FFFFF) plus
        ;; PDE[769..LAST_KERNEL_PDE-1] pointing at the per-4 MB PTs the
        ;; loop above just allocated.  Build a fresh 4 KB
        ;; `kernel_idle_pd` from a frame_alloc'd frame, copy-image the
        ;; boot PD's kernel-half PDEs (FIRST_KERNEL_PDE..1023) into it, leave the
        ;; user-half (0..767) zero, switch CR3 to it, and free the
        ;; boot PD.  The idle PD takes over both roles the boot PD
        ;; had:
        ;;   * canonical kernel-half PDE source for `address_space_create`
        ;;   * CR3-swap target during `sys_exit` / kill-path teardown
        ;;     (which cannot run on the dying user PD it is about to
        ;;     frame_free).
        ;; The idle PD lives wherever the bitmap allocator returned a
        ;; frame — typically right after the kernel PTs allocated
        ;; above — so it isn't pinned in the kernel-side reserved
        ;; cluster.  After this block the boot PD's 4 KB cluster slot
        ;; is just another conventional frame the bitmap allocator can
        ;; hand out for user pages.
        ;;
        ;; PDE constants (768 = ADDRESS_SPACE_USER_PDE_COUNT,
        ;;                256 = ADDRESS_SPACE_KERNEL_PDE_COUNT) are
        ;; spelled as literals here because address_space.asm's
        ;; `%define`s aren't visible until its `%include` later in
        ;; kernel.asm.
        call frame_alloc
        jc .panic
        mov [kernel_idle_pd_phys], eax
        mov edi, eax
        add edi, DIRECT_MAP_BASE
        ;; Zero the entire frame (PDEs 0..1023).
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi
        ;; Copy boot PD's kernel-half (PDEs FIRST_KERNEL_PDE..1023)
        ;; into the idle PD.  At FIRST_KERNEL_PDE = 1022 that's two
        ;; entries: the direct-map PT and the (still-zero) kmap
        ;; window slot which kmap_init populates next.
        mov esi, DIRECT_MAP_BASE + BOOT_PD_PHYS + FIRST_KERNEL_PDE * 4
        add edi, FIRST_KERNEL_PDE * 4
        mov ecx, 1024 - FIRST_KERNEL_PDE
        rep movsd

        ;; Switch CR3 to the idle PD, then free the boot PD.  The CR3
        ;; reload flushes the TLB, retiring any cached BOOT_PD walks
        ;; before the frame_free returns the boot PD's frame to the
        ;; bitmap pool.
        mov eax, [kernel_idle_pd_phys]
        mov cr3, eax
        mov eax, BOOT_PD_PHYS
        call frame_free

        ;; --- Bring up the kmap window ---
        ;;
        ;; Allocates one frame for the window PT and installs it at
        ;; idle_pd[KMAP_WINDOW_PDE] (= 1023).  Every per-program PD
        ;; built afterward inherits PDE 1023 verbatim through
        ;; address_space_create's kernel-half copy-image, so the
        ;; window is reachable from every CR3.  Must run before the
        ;; first user PD is built (i.e. before shell_reload), and
        ;; before any kmap_map call could land on a slot — which
        ;; means before vdso_install in protected_mode_entry below
        ;; (the first kmap-using callsite past this point).
        call kmap_init

        ;; Continue with the existing post-flip init: TSS / IDT IRQ
        ;; gates / drivers / VFS / NIC / banner / shell.  Lives in
        ;; entry.asm's `protected_mode_entry`, trimmed to skip the
        ;; segment / ESP / lidt work `high_entry` already performed.
        ;; Programs run in private per-program PDs built by
        ;; `address_space_create` from `program_enter`; the idle PD's
        ;; user half is zero-filled so kernel-mode code running on it
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

%include "memory_management/access.asm"
%include "memory_management/address_space.asm"
%include "memory_management/frame.asm"
%include "memory_management/kmap.asm"
%include "drivers/ata.kasm"
%include "drivers/console.kasm"
%include "drivers/fdc.kasm"
%include "drivers/ne2k.kasm"
%include "drivers/ps2.kasm"
%include "drivers/rtc.kasm"
%include "drivers/sb16.kasm"
%include "drivers/serial.kasm"
%include "drivers/vga.kasm"
%include "entry.asm"
%include "fs/block.asm"
%include "fs/fd.kasm"
%include "fs/vfs.kasm"
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
        ;; FUNCTION_* constants in constants.asm.  Each per-program
        ;; PD aliases the shared vDSO frame at user-virt 0x10000 with
        ;; the AVL[0] PTE_SHARED bit so `address_space_destroy` skips
        ;; it.
        align 4
vdso_image:
        incbin "vdso.bin"
vdso_image_end:

kernel_end:
