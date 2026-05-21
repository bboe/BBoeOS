;;; ------------------------------------------------------------------------
;;; kernel.asm — high-half kernel binary (org 0xC0020000).
;;;
;;; Loaded onto disk after boot.bin and read into physical 0x20000 by
;;; boot.asm's real-mode INT 13h.  The phys load address sits in
;;; conventional RAM (above the libbboeos target at 0x10000, below the VGA
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
;;; libbboeos PTE marked with the AVL[0] PTE_SHARED bit so
;;; `address_space_destroy` skips frame_free on it.  Programs run
;;; with PROGRAM_BASE=0x08048000 and USER_STACK_TOP=0x40000000 (Linux
;;; ELF convention); the libbboeos (0x10000) stays at low user-virt and
;;; reaches the program through the per-program PD's first PT.
;;; ------------------------------------------------------------------------

        section .text progbits vstart=0FF820000h
        bits 32
        %include "constants.asm"
        %include "irq_tail.inc"

        ;; Declare the .bss section up front and anchor `kernel_bss_start:`
        ;; at its very first byte, before the kasm %includes below.  The
        ;; kasms emitted by cc.py emit their zero-init globals as `resb`
        ;; reservations inside this same `.bss` section, and NASM lays
        ;; them out in source-encounter order — so they fall between
        ;; `kernel_bss_start:` here and `kernel_bss_end:` at the tail
        ;; of this file.  `high_entry`'s zero loop then covers every
        ;; resb in the kernel, kernel-asm and cc.py alike.
        section .bss nobits follows=.text align=4
kernel_bss_start:
        section .text

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
        ;;   libbboeos target at phys 0x10000          (1 page, mapped per-PD)
        ;;   kernel.bin at KERNEL_LOAD_PHYS       (image; var size)
        ;;   KERNEL_RESERVED_BASE                 (page-aligned post-image)
        ;;     kernel_stack                       (KERNEL_STACK_BYTES = 1 KB; slot_a)
        ;;     kernel_stack_b                     (KERNEL_STACK_BYTES = 1 KB; pipeline cmd1)
        ;;     kernel_stack_c                     (KERNEL_STACK_BYTES = 1 KB; pipeline cmd2)
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
        ;; sector_buffer is a `uint8_t *` pointer cell in kernel/fs/vfs.c
        ;; BSS, populated at boot by `vfs_init` with the kernel-virt of
        ;; `sector_buffer_storage` — a 512 B `resb` block in this file's
        ;; .bss section (see kernel_bss_start..kernel_bss_end below).
        ;; bbfs.asm / ext2.asm callers indirect through `[sector_buffer]`
        ;; to load the base, then `[reg + offset]`.  No frame_alloc;
        ;; the storage is unconditional and tiny.
        ;;
        ;; ext2_sd_buffer is the runtime pointer to a 4 KB frame
        ;; allocated by `ext2_init` when (and only when) the ext2
        ;; superblock magic matches.  1 KB of the frame is used as
        ;; ext2_search_blk's sliding 2-sector directory window; the
        ;; upper 3 KB sits unused (no sub-page allocator).  bbfs
        ;; systems never spend this frame.  `ext2_init` treats
        ;; frame_alloc failure here as a hard panic — same recovery
        ;; story as the deleted vfs_init_scratch.vis_oom.
        ;;
        ;; net_receive_buffer / net_transmit_buffer / arp_table /
        ;; udp_buffer share one 4 KB NIC scratch frame allocated by
        ;; `network_initialize` only when the NIC is detected;
        ;; sessions booted without a NIC never spend the frame.
        %ifndef KERNEL_RESERVED_BASE
        %define KERNEL_RESERVED_BASE 0x40000
        %endif
        ;; Page-align up: BOOT_PD lives in the next 4 KB-aligned slot
        ;; above the last kernel stack.  At 4 KB stacks the alignment
        ;; was free (3 × 4 KB = 3 pages); at 1 KB stacks (3 × 1 KB =
        ;; 0xC00, sub-page) we pay 0x400 = 1 KB of padding.  The pad
        ;; bytes sit in the bitmap allocator's free pool — they are
        ;; not part of any reserved region — so this is purely a
        ;; physical-address alignment cost, not a memory cost.
        BOOT_PD_PHYS             equ (KERNEL_STACK_C_TOP_PHYS + 0xFFF) & ~0xFFF
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
        KERNEL_STACK_BYTES       equ 0x400                               ; 1 KB (peak measured ~412 B; ~2.5× margin; 0xDEADBEEF poison-fill at boot catches overruns)
        KERNEL_STACK_PHYS        equ KERNEL_RESERVED_BASE
        KERNEL_STACK_TOP_PHYS    equ KERNEL_STACK_PHYS + KERNEL_STACK_BYTES
        ;; Slot_b and slot_c each get their own 4 KB kernel stack in the
        ;; same reserved-memory region as the shell's `kernel_stack` so
        ;; a slot that yields mid-syscall can preserve its kernel call
        ;; chain across cooperative switches.  These live above
        ;; KERNEL_RESERVED_BASE alongside the shell stack — not in
        ;; entry.asm BSS — so the 8 KB of zero bytes don't bloat
        ;; kernel.bin and shift the user frame pool downward (which
        ;; would invalidate the page-precise BIGBSS_PAGES tripwire).
        KERNEL_STACK_B_PHYS      equ KERNEL_STACK_TOP_PHYS
        KERNEL_STACK_B_TOP_PHYS  equ KERNEL_STACK_B_PHYS + KERNEL_STACK_BYTES
        KERNEL_STACK_C_PHYS      equ KERNEL_STACK_B_TOP_PHYS
        KERNEL_STACK_C_TOP_PHYS  equ KERNEL_STACK_C_PHYS + KERNEL_STACK_BYTES
        LAST_KERNEL_PDE          equ 1023        ; PDEs [768..1022]: 255 entries × 4 MB = ~1020 MB direct map; PDE 1023 reserved for the kmap window
        frame_bitmap             equ DIRECT_MAP_BASE + FRAME_BITMAP_PHYS
        kernel_stack             equ DIRECT_MAP_BASE + KERNEL_STACK_PHYS
        kernel_stack_top         equ DIRECT_MAP_BASE + KERNEL_STACK_TOP_PHYS
        ;; Slot_a (the shell) reuses the existing kernel stack — alias the
        ;; "_a_top" symbol to the original `kernel_stack_top` so shell_reload
        ;; can initialize all three slots' kernel_stack_top fields uniformly.
        kernel_stack_a_top       equ kernel_stack_top
        kernel_stack_b           equ DIRECT_MAP_BASE + KERNEL_STACK_B_PHYS
        kernel_stack_b_top       equ DIRECT_MAP_BASE + KERNEL_STACK_B_TOP_PHYS
        kernel_stack_c           equ DIRECT_MAP_BASE + KERNEL_STACK_C_PHYS
        kernel_stack_c_top       equ DIRECT_MAP_BASE + KERNEL_STACK_C_TOP_PHYS

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

        ;; --- Zero the kernel BSS ---
        ;;
        ;; `.bss` is a `nobits` section: the bootloader loaded no bytes
        ;; for it, so without this fill, reads from program_state_a /
        ;; tss_data / pipeline_active / etc. return whatever the boot PD
        ;; left in those frames.  Runs after the stack switch (so we
        ;; have a safe ESP) and after the stack poison-fill (which uses
        ;; the same EAX/ECX/EDI register convention), but BEFORE `lidt`
        ;; — the IDT install reads no BSS, but anything past it might
        ;; (e.g. frame_init writes the bitmap; address_space_create
        ;; reads kernel_idle_pd_phys).
        ;;
        ;; The (end - start + 3) / 4 ceiling-divide handles BSS sizes
        ;; that aren't a multiple of 4; rep stosd will overshoot by up
        ;; to 3 bytes into the next page, which is fine — the next page
        ;; is either still in kernel direct map or unmapped (which would
        ;; have faulted on the first byte we wrote anyway, so it can't
        ;; be unmapped if we got this far).  kernel_bss_end is aligned
        ;; up to 4 in practice (last entry is `tss_data resb 104`), so
        ;; the overshoot is zero, but the form is defensive.
        mov edi, kernel_bss_start
        mov ecx, (kernel_bss_end - kernel_bss_start + 3) / 4
        xor eax, eax
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
        ;;   1. libbboeos target frame at phys 0x10000.  One 4 KB page.
        ;;      The libbboeos is mapped into every per-program PD as a
        ;;      shared user code page, so its phys location must
        ;;      stay pinned.
        ;;   2. Kernel image and KERNEL_RESERVED_BASE region:
        ;;      KERNEL_LOAD_PHYS..(FRAME_BITMAP_PHYS + frame_bitmap_bytes).
        ;;      Covers the kernel image, kernel stack, boot PD, first
        ;;      kernel PT, and the runtime-sized frame_bitmap.
        mov eax, 0x10000
        mov ecx, 0x1000                 ; libbboeos target page
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
        ;; means before libbboeos_install in protected_mode_entry below
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
%include "drivers/opl3.kasm"
%include "drivers/ps2.kasm"
%include "drivers/rtc.kasm"
%include "drivers/sb16.kasm"
%include "drivers/serial.kasm"
%include "drivers/vga.kasm"
%include "entry.asm"
%include "fs/block.asm"
%include "fs/fd.kasm"
%include "fs/pipe.kasm"
%include "fs/sector_cache.kasm"
%include "fs/vfs.kasm"
%include "net/net.asm"
%include "signal.kasm"
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

        ;; The libbboeos shared blob (FUNCTION_TABLE jump block + shared_*
        ;; helper bodies + FUNCTION_POINTER_TABLE) used to be incbin'd
        ;; here, but it now ships as `lib/libbboeos` on the disk image.
        ;; `libbboeos_install` in entry.asm reads it at boot, copies it
        ;; into a freshly-allocated frame, and maps that frame with
        ;; PTE_SHARED at user-virt FUNCTION_TABLE (0x00010000) in
        ;; every per-program PD.  Decoupling libbboeos from kernel.bin
        ;; keeps the kernel image smaller and lets libbboeos grow
        ;; (eventually past 4 KB) without recompiling the kernel.

kernel_end:

;;; -------------------------------------------------------------------------
;;; Kernel BSS — zero-initialized data, NOT emitted to kernel.bin.
;;;
;;; All `resX` reservations across the kernel live here.  NASM places the
;;; section immediately after .text in the load image's virtual address
;;; space (`follows=.text`), but `nobits` means no bytes ride on disk.
;;; `high_entry` zero-fills the range [kernel_bss_start, kernel_bss_end)
;;; before any code reads from it; the bootloader will NOT have loaded
;;; zeros here, so a missed init = garbage reads.
;;;
;;; The `make_os.sh` build script reads kernel_bss_start / kernel_bss_end
;;; from `build/kernel.map` (emitted by the [map symbols] directive below)
;;; and adds the BSS extent to KERNEL_RESERVED_BASE so the post-kernel
;;; cluster (kernel stacks, boot PD, PT, bitmap) starts above BSS, not
;;; above the on-disk image end.
;;; -------------------------------------------------------------------------

[map symbols build/kernel.map]

section .bss
        ;; cc.py-emitted kasm `resb` reservations land between
        ;; `kernel_bss_start:` (top of file) and here; some are odd
        ;; sizes (`resb 1` byte scalars, `resb 6` mac_address, etc.),
        ;; so the running offset is not guaranteed 4-aligned at this
        ;; point.  Re-align before the kernel.asm-internal labels —
        ;; every one of them is `resd 1` / `resd N` / `resb 4·N` and
        ;; expects 4-aligned access.
        align 4

;;; Labels below are strict-alphabetical (matching the codebase's `equ`
;;; convention).  Every reservation is `resd 1`, `resd N`, or `resb N`
;;; with N a multiple of 4, so the section stays 4-aligned end-to-end
;;; from this `align 4` without per-label re-aligns.
;;;
;;; `kernel_bss_start:` is anchored at the *start* of .bss at the top
;;; of this file (before the kasm %includes), so the cc.py-emitted
;;; resb reservations from build/kernel-c/**/*.kasm fall between the
;;; start label and `kernel_bss_end:` below.  All of them — kernel-asm
;;; and cc.py-emitted alike — get zeroed by the boot loop in
;;; `high_entry`.

        ;; Physical address of the kernel idle PD — a long-lived 4 KB
        ;; kernel-only page directory built in `high_entry` by
        ;; copy-imaging the boot PD's kernel half (PDEs FIRST_KERNEL_PDE..1023)
        ;; into a fresh frame_alloc'd frame and leaving the user half
        ;; (PDEs 0..767) zero.  Used as the canonical kernel-half PDE
        ;; source for `address_space_create`, as CR3 between programs
        ;; (post sys_exit / kill, before program_enter swaps in the
        ;; next program's PD), and as the CR3-swap target during
        ;; `address_space_destroy` (which cannot run on the dying PD
        ;; it is about to frame_free).  Replaces the boot PD's
        ;; permanent-frame role; the boot PD's frame is freed once
        ;; the idle PD takes over, returning a 4 KB conventional
        ;; frame to the bitmap pool for user pages.
kernel_idle_pd_phys      resd 1

        ;; Phys of the last loaded binary frame, used by program_enter
        ;; for the post-stream BSS-trailer peek.
last_binary_frame_phys   resd 1

        ;; OOM-recovery: 1 while shell_reload is bringing up the shell.
        ;; An OOM in that window is fatal (no fallback); user-program
        ;; loads run with the flag clear and recover by printing a
        ;; message and re-entering shell_reload.  Paired with
        ;; pending_frame_phys below for the unwind contract.
loading_shell_flag       resd 1

        ;; parent_iret_frame snapshots the parent's pushad+iret kernel-stack
        ;; frame (52 bytes = 13 dwords) at sys_exec entry.
parent_iret_frame        resd 13

        ;; Non-null while a child is live; consumed by child_terminate
        ;; to restore the parent on sys_exit.
parent_program_state     resd 1

        ;; User-virt char** that stage_user_argv (called from
        ;; build_child_program_state) walks under the caller's PD to copy
        ;; argv strings directly onto the new program's user stack via
        ;; kmap.  Set by sys_exec / sys_pipeline2 before each child
        ;; build; 0 means "no args" (the boot / sys_exit shell-reload
        ;; path, or an explicit NULL argv from a user program).
        ;; Callers must have validated the array with .validate_user_argv
        ;; first so stage_user_argv can trust every dereference.
pending_argv_user_ptr    resd 1

        ;; Set immediately after every frame_alloc that has not yet been
        ;; mapped via address_space_map_page; the .oom handler frees it
        ;; before tearing down the partial PD.  Paired with
        ;; loading_shell_flag above for the OOM-recovery contract.
pending_frame_phys       resd 1

        ;; Active pipeline's pipe-pool index.  Set by sys_pipeline2 in
        ;; entry, consumed by both child-build steps to install the
        ;; matching FD_TYPE_PIPE_R / FD_TYPE_PIPE_W fds on slot_b /
        ;; slot_c.  Holds the index verbatim — pool index 0 is valid,
        ;; so the "is a pipeline active?" check uses pipeline_active
        ;; below, not this field.  The error-unwind paths read this
        ;; to release the pool slot when a partial build fails.
pending_pipeline_pipe    resd 1

        ;; Set non-zero by sys_pipeline2 between "both children built"
        ;; and "both children exited" — used by child_terminate's
        ;; sys_exit path to detect a pipeline-child exit vs. a regular
        ;; sys_exec child exit.  The pipeline-child branch routes
        ;; sys_exit through kernel_yield (no parent_iret_frame
        ;; restore); the non-pipeline branch keeps the existing
        ;; parent-restore behavior.
pipeline_active          resd 1

        ;; sys_pipeline2 sets this to 1 once slot_b is fully built (PD
        ;; allocated, fd_table populated, pipe writer end installed),
        ;; and back to 0 once both children are STATE_RUNNING.
        ;; spawn_failed_unwind consults it: if non-zero when slot_c's
        ;; build_child_program_state OOMs, the normal slot_c teardown
        ;; is followed by .pipeline_unwind_slot_b so slot_b's PD, the
        ;; writer fd, and the pipe pool slot don't leak.  Values:
        ;; 0 = no pipeline build in progress; 1 = slot_b built (needs
        ;; teardown on slot_c OOM).
pipeline_partial_state   resd 1

        ;; Kernel-side fd struct used by program_enter to stream the
        ;; program binary directly from disk into per-program user
        ;; frames (sector-by-sector via vfs_read_sec).  Sized to
        ;; FD_ENTRY_SIZE so the FD_OFFSET_* layout matches the user fd
        ;; table, even though this slot lives outside it.  Only one
        ;; program loads at a time, so a single static slot suffices.
program_fd               resb FD_ENTRY_SIZE

        ;; program_state_a holds the running program's PROGRAM_STATE
        ;; slot.  current_program_state (initialized in .text to point
        ;; here) is pre-set to it so the PIT handler is safe before
        ;; shell_reload runs; shell_reload also sets it (redundant but
        ;; harmless).  Signal delivery state — pending bits
        ;; (PENDING_SIGINT, PENDING_SIGPIPE, PENDING_SIGALRM), the
        ;; re-entry guard (IN_SIGNAL_HANDLER), and the alarm deadline /
        ;; interval — lives inside this slot at the
        ;; PROGRAM_STATE_OFFSET_* fields; program_enter resets them on
        ;; every load.
program_state_a          resb PROGRAM_STATE_SIZE

        ;; Second slot for the child while a parent is suspended;
        ;; completes the pair alongside program_state_a.
program_state_b          resb PROGRAM_STATE_SIZE

        ;; Third slot — second pipeline child.  Used by sys_pipeline2
        ;; so the shell + two cooperatively-scheduled pipeline commands
        ;; all have their own program_state.  Stays zero
        ;; (STATE_BLOCKED_READ-ish from BSS, pd_phys=0) outside of an
        ;; active pipeline; shell_reload re-zeroes it on every reload.
program_state_c          resb PROGRAM_STATE_SIZE

        ;; FS sector scratch — 512 B used by every disk read on both
        ;; bbfs and ext2.  Address is published to consumers via the
        ;; existing `sector_buffer` pointer cell (cc.py-side
        ;; `_g_sector_buffer`); `vfs_init` writes the address of this
        ;; storage block into the pointer cell at boot.  Pre-PR #364
        ;; this lived in a `vfs_init`-allocated 4 KB scratch frame; the
        ;; frame_alloc went away with that PR — bbfs no longer pays for
        ;; a scratch frame at all, and ext2 pays for its own frame from
        ;; ext2_init for the sliding directory window.
sector_buffer_storage    resb 512

        ;; Phys of the topmost user stack frame (virt 0xFF7FF000) — the
        ;; one that holds USER_STACK_TOP-1 and below.  Captured during
        ;; build_child_program_state's stack-mapping loop and consumed
        ;; by stage_user_argv to write the Linux argv/envp/argc frame.
topmost_stack_frame_phys resd 1

        ;; 32-bit TSS.  Only SS0/ESP0/IOPB-offset are populated (in
        ;; protected_mode_entry); all other fields stay zero because we
        ;; don't use hardware task switching.  Sized to the 104-byte
        ;; standard layout so the IOPB-past-limit trick parks I/O.
tss_data                 resb 104

        ;; PROGRAM_BASE + binsize + bsssize, page-aligned up — used by
        ;; program_enter to bound the user text+BSS extent during the
        ;; stack-mapping / argv-staging passes.
user_image_end           resd 1

        ;; Phys of the shared libbboeos code frames; build_child_program_state
        ;; aliases the first libbboeos_page_count entries into every per-program PD
        ;; at consecutive user-virts FUNCTION_TABLE, FUNCTION_TABLE + 0x1000,
        ;; ... so libbboeos can grow past one page.  Sized at compile time by
        ;; LIBBBOEOS_PAGE_COUNT_MAX; only the first libbboeos_page_count slots are live.
libbboeos_code_phys           resd LIBBBOEOS_PAGE_COUNT_MAX

        ;; Number of 4 KB frames libbboeos_install actually populated this boot.
        ;; Set to ceil(libbboeos_size / 4096); read by build_child_program_state
        ;; to bound the per-program libbboeos map loop.
libbboeos_page_count          resd 1

        ;; Current user-virt during page-walk loops in
        ;; build_child_program_state / address_space_map_page callers.
virt_cursor              resd 1

kernel_bss_end:
