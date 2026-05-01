;;; ------------------------------------------------------------------------
;;; memory_management/kmap.asm — kernel temporary mapping window.
;;;
;;; The kernel direct map at virt 0xC0000000.. covers only the first
;;; FRAME_DIRECT_MAP_LIMIT (= (LAST_KERNEL_PDE - FIRST_KERNEL_PDE) *
;;; 4 MB ≈ 1020 MB at LAST_KERNEL_PDE = 1023) of installed RAM, which
;;; is what the kernel can reach without an extra page-table walk.
;;; Frames at higher physical addresses still appear in the bitmap
;;; (clamped at FRAME_PHYSICAL_LIMIT, ~4 GB) but have no direct-map
;;; alias.  The kmap window — one PT installed at PDE
;;; KMAP_WINDOW_PDE in every kernel-half PD — gives the kernel a
;;; small pool of demand-mapped slots so it can zero-fill / copy
;;; into / read out of those frames.
;;;
;;; Public (CPL=0 only):
;;;   kmap_init                                      build the window PT,
;;;                                                  install it at
;;;                                                  kernel_idle_pd[KMAP_WINDOW_PDE]
;;;                                                  (per-program PDs
;;;                                                  inherit it via
;;;                                                  address_space_create's
;;;                                                  kernel-half copy-image)
;;;   kmap_map(eax = phys)         -> eax = kvirt    fast path returns
;;;                                                  phys + DIRECT_MAP_BASE
;;;                                                  when phys is below
;;;                                                  FRAME_DIRECT_MAP_LIMIT;
;;;                                                  otherwise consumes a
;;;                                                  slot, writes a PTE,
;;;                                                  invlpgs, and returns
;;;                                                  the slot's kvirt
;;;   kmap_unmap(eax = kvirt)                        no-op when kvirt is in
;;;                                                  the direct-map range;
;;;                                                  otherwise clears the
;;;                                                  window PTE, invlpgs,
;;;                                                  and frees the slot
;;;
;;; All three routines preserve every register the caller passed in
;;; (success and failure paths both restore via the prologue/epilogue
;;; pop chain).  kmap_map's only return value is in EAX; the others
;;; communicate solely via side effects.
;;;
;;; The slot count (KMAP_SLOT_COUNT = 4) is sized for the deepest
;;; concurrent nesting in the tree: address_space_destroy walks a
;;; PD (one slot) and, per present PDE, walks its PT (a second slot)
;;; before unmapping it.  Single-CPU, no preemption, no recursive
;;; map-without-unmap, so a small fixed slot pool suffices.  Slot
;;; exhaustion panics — that path indicates a kernel bug, not a
;;; runtime allocation failure.
;;;
;;; Convention: every "phys → kernel-virt to read/write the page"
;;; path pairs kmap_map with a matching kmap_unmap.  Even when a
;;; caller knows its frame is below FRAME_DIRECT_MAP_LIMIT (e.g.
;;; vdso_install at boot), going through the helper keeps the
;;; calling code uniform — the fast path adds DIRECT_MAP_BASE and
;;; returns without touching the slot pool.
;;; ------------------------------------------------------------------------

%define KMAP_PTE_FLAGS          0x003                   ; P | RW (kernel-only)
%define KMAP_SLOT_COUNT         4
%define KMAP_WINDOW_PDE         1023                    ; reserved PDE; LAST_KERNEL_PDE caps the direct-map auto-grow at 1023
%define KMAP_WINDOW_VIRT        (KMAP_WINDOW_PDE * 0x400000)    ; 0xFFC00000

kmap_init:
        ;; Allocate one frame for the window PT, zero it via the
        ;; kernel direct map (the freshly-allocated frame is in low
        ;; conventional RAM at boot — kmap_init runs before any high
        ;; frames are handed out, so no chicken-and-egg problem),
        ;; and install it at idle_pd[KMAP_WINDOW_PDE].  Every
        ;; per-program PD built afterward inherits PDE
        ;; KMAP_WINDOW_PDE from the idle PD via the kernel-half
        ;; copy-image in address_space_create, so kmap_* works from
        ;; every CR3.
        push eax
        push ebx
        push ecx
        push edi
        call frame_alloc
        jc .panic
        mov [kmap_pt_phys], eax
        mov edi, eax
        add edi, DIRECT_MAP_BASE
        push edi
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop edi
        ;; Install the PT at idle_pd[KMAP_WINDOW_PDE].  CR3 already
        ;; points at the idle PD by the time high_entry calls us.
        mov ebx, [kernel_idle_pd_phys]
        add ebx, DIRECT_MAP_BASE
        mov eax, [kmap_pt_phys]
        or eax, KMAP_PTE_FLAGS
        mov [ebx + KMAP_WINDOW_PDE * 4], eax
        ;; The PDE write is on a fresh kernel-half slot (PDE 1023 was
        ;; left zero by the direct-map auto-grow loop, which now caps
        ;; at LAST_KERNEL_PDE = 1023).  No previous TLB entry exists
        ;; for the kmap window range, so no flush is needed.
        mov dword [kmap_slot_bitmap], 0
        pop edi
        pop ecx
        pop ebx
        pop eax
        ret
.panic:
        mov dx, 0x3F8
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

kmap_map:
        ;; EAX = phys; returns EAX = kernel-virt mapping of that frame.
        ;; Fast path: phys < FRAME_DIRECT_MAP_LIMIT → return
        ;; phys + DIRECT_MAP_BASE without touching the slot pool.
        ;; Slow path: claim a free slot in the window, write the PTE,
        ;; invlpg the slot's virt, return that virt.  Panics on slot
        ;; exhaustion (kernel bug — exceeds KMAP_SLOT_COUNT concurrent
        ;; mappings).
        cmp eax, FRAME_DIRECT_MAP_LIMIT
        jae .slow
        add eax, DIRECT_MAP_BASE
        ret
.slow:
        push ebx
        push ecx
        push edx
        ;; Find the lowest free slot.  EDX = ~bitmap; bsf finds the
        ;; lowest set bit (= lowest 0 bit in the original bitmap).
        ;; ZF=1 means bitmap was all-ones (impossible with 4 slots and
        ;; the upper 28 bits of the dword always zero), but the cmp
        ;; against KMAP_SLOT_COUNT below catches that anyway.
        mov edx, [kmap_slot_bitmap]
        not edx
        bsf ecx, edx
        jz .panic
        cmp ecx, KMAP_SLOT_COUNT
        jae .panic
        ;; Set the slot bit.
        mov ebx, 1
        push ecx
        shl ebx, cl
        pop ecx
        or [kmap_slot_bitmap], ebx
        ;; Compute slot kernel-virt = KMAP_WINDOW_VIRT + slot * 0x1000.
        mov ebx, ecx
        shl ebx, 12
        add ebx, KMAP_WINDOW_VIRT
        ;; Write the PTE at kmap_pt_kvirt + slot * 4.
        mov edx, [kmap_pt_phys]
        add edx, DIRECT_MAP_BASE
        or eax, KMAP_PTE_FLAGS
        mov [edx + ecx*4], eax
        ;; Invalidate any stale TLB entry for this slot.
        invlpg [ebx]
        mov eax, ebx
        pop edx
        pop ecx
        pop ebx
        ret
.panic:
        mov dx, 0x3F8
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

kmap_unmap:
        ;; EAX = kernel-virt previously returned by kmap_map.
        ;; Fast path: addr in the direct-map range → no-op (the
        ;; matching kmap_map didn't claim a slot).  Slow path: clear
        ;; the slot's PTE, invlpg, free the slot bit.
        ;; The direct-map range is [DIRECT_MAP_BASE,
        ;; DIRECT_MAP_BASE + FRAME_DIRECT_MAP_LIMIT); everything else
        ;; was mapped through the slot pool.
        push ebx
        push ecx
        push edx
        mov ebx, eax
        sub ebx, DIRECT_MAP_BASE
        cmp ebx, FRAME_DIRECT_MAP_LIMIT
        jb .done
        ;; Slot index = (kvirt - KMAP_WINDOW_VIRT) >> 12.
        mov ecx, eax
        sub ecx, KMAP_WINDOW_VIRT
        shr ecx, 12
        cmp ecx, KMAP_SLOT_COUNT
        jae .panic
        ;; Clear the PTE.
        mov edx, [kmap_pt_phys]
        add edx, DIRECT_MAP_BASE
        mov dword [edx + ecx*4], 0
        invlpg [eax]
        ;; Clear the slot bit.
        mov ebx, 1
        push ecx
        shl ebx, cl
        not ebx
        pop ecx
        and [kmap_slot_bitmap], ebx
.done:
        pop edx
        pop ecx
        pop ebx
        ret
.panic:
        mov dx, 0x3F8
        mov al, '!'
        out dx, al
        cli
        hlt
        jmp $-1

        align 4
        ;; Phys of the kmap window PT (allocated by kmap_init).  The
        ;; PT itself sits in low conventional RAM (the bitmap
        ;; allocator's first hits go to low frames), so it's reachable
        ;; through the direct map for the bitmap-bit / PTE accesses
        ;; below — kmap_init does not need to recursively kmap its
        ;; own backing PT.
kmap_pt_phys            dd 0
        ;; Slot in-use bitmap.  Bit i set = slot i currently mapped.
        ;; Only the low KMAP_SLOT_COUNT bits are ever written; the
        ;; upper bits stay zero and `cmp ecx, KMAP_SLOT_COUNT`
        ;; rejects bsf hits in that range as panic-worthy.
kmap_slot_bitmap        dd 0
