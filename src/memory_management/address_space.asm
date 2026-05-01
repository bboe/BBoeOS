;;; ------------------------------------------------------------------------
;;; memory_management/address_space.asm — per-program address-space helpers.
;;;
;;; Builds and tears down per-program user page directories.  Kernel-half
;;; PDEs (768..1023, sized at boot to cover installed RAM up to ~1020 MB
;;; through PDEs 768..1022 plus the kmap window at PDE 1023) are copied
;;; verbatim from `kernel_idle_pd`'s kernel half at address_space_create
;;; time and never modified afterward — that invariant is what lets us
;;; avoid fan-out updates when the kernel installs a new kernel-half
;;; mapping.  `kernel_idle_pd` is built once by `high_entry` (after the
;;; kernel-PT-alloc loop) and serves both as the canonical PDE source
;;; and as the CR3 target between programs.
;;;
;;; All PD / PT reads and writes go through `kmap_map` /
;;; `kmap_unmap` (memory_management/kmap.asm) so PD or PT frames
;;; allocated above the direct-map ceiling
;;; (FRAME_DIRECT_MAP_LIMIT, ~1020 MB) remain reachable.  Frames
;;; below the ceiling fast-path through the direct map and don't
;;; consume a kmap slot — the helper handles both transparently.
;;;
;;; Public (CPL=0 only):
;;;   address_space_create()                  -> EAX = pd_phys, CF on OOM
;;;   address_space_destroy(eax = pd_phys)     free user PTs and pages, then PD
;;;   address_space_map_page(eax = pd_phys,
;;;                          ebx = user_virt,
;;;                          ecx = phys,
;;;                          edx = flags)      install / replace PTE; CF on OOM
;;;   address_space_unmap_page(eax = pd_phys,
;;;                            ebx = user_virt) clear PTE; invlpg if pd is current
;;;
;;; All four routines preserve every register the caller passed in
;;; (success and failure paths both restore via the prologue/epilogue
;;; pop chain).  The success-path return value lives in EAX for
;;; address_space_create only; the others communicate solely via CF.
;;;
;;; Called from `program_enter` (entry.asm), which builds a fresh PD
;;; per program load and tears it down on `sys_exit`.
;;; ------------------------------------------------------------------------

%define ADDRESS_SPACE_DIRECT_MAP_BASE   0xC0000000
%define ADDRESS_SPACE_KERNEL_PDE_COUNT  256             ; PDEs 768..1023 are kernel-half
%define ADDRESS_SPACE_PDE_PRESENT       0x001
%define ADDRESS_SPACE_PDE_RW            0x002
%define ADDRESS_SPACE_PDE_USER          0x004
%define ADDRESS_SPACE_PDE_USER_RW       (ADDRESS_SPACE_PDE_PRESENT | ADDRESS_SPACE_PDE_RW | ADDRESS_SPACE_PDE_USER)
%define ADDRESS_SPACE_PTE_SHARED        0x200           ; AVL[0]: frame is shared, address_space_destroy skips frame_free
%define ADDRESS_SPACE_USER_PDE_COUNT    768             ; PDEs 0..767 are user-half

address_space_create:
        ;; Allocate one frame, zero it, then copy the top-256 PDEs
        ;; from `kernel_idle_pd` into the new PD's kernel-half slot.
        ;; Both PDs are accessed through kmap so a high-physical PD
        ;; frame stays reachable.  The idle PD itself was allocated
        ;; in `high_entry` before any high frames were handed out, so
        ;; its kmap_map fast-paths to the direct-map alias.  Output:
        ;; EAX = pd_phys, CF clear on success; CF set on OOM.
        push ebx
        push ecx
        push edi
        push esi
        call frame_alloc
        jc .oom
        ;; EAX = new pd_phys.  Save it for the return; map the new PD.
        push eax                                ; [esp+4] = pd_phys (return value)
        call kmap_map                           ; EAX = pd_kvirt
        push eax                                ; [esp+0] = pd_kvirt (for unmap)
        ;; Zero all 1024 PDEs.
        mov edi, eax
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        ;; Copy the kernel-half PDEs (768..1023) from kernel_idle_pd.
        mov eax, [kernel_idle_pd_phys]
        call kmap_map                           ; EAX = idle_kvirt
        mov esi, eax
        add esi, ADDRESS_SPACE_USER_PDE_COUNT * 4
        mov edi, [esp]                          ; new pd_kvirt
        add edi, ADDRESS_SPACE_USER_PDE_COUNT * 4
        mov ecx, ADDRESS_SPACE_KERNEL_PDE_COUNT
        rep movsd
        ;; Reload idle_kvirt for the unmap.  ESI was advanced past
        ;; the copy; back it up to the original kvirt.
        mov eax, esi
        sub eax, ADDRESS_SPACE_USER_PDE_COUNT * 4
        sub eax, ADDRESS_SPACE_KERNEL_PDE_COUNT * 4
        call kmap_unmap                         ; release idle PD kmap (fast-path no-op)
        pop eax                                 ; new pd_kvirt
        call kmap_unmap                         ; release new PD kmap
        pop eax                                 ; pd_phys (return value)
        clc
        pop esi
        pop edi
        pop ecx
        pop ebx
        ret
.oom:
        stc
        pop esi
        pop edi
        pop ecx
        pop ebx
        ret

address_space_destroy:
        ;; EAX = pd_phys.  Walks PDEs 0..767 (user half) through a
        ;; kmap_map alias of the PD frame.  For each present PDE,
        ;; kmap_maps the PT, frees every present user-page frame
        ;; (skipping ADDRESS_SPACE_PTE_SHARED entries — vDSO and
        ;; friends live in shared tables managed by the kernel),
        ;; then unmaps and frees the PT frame itself.  Finally
        ;; unmaps and frees the PD frame.  Caller must not have
        ;; pd_phys loaded in CR3 — the `sys_exit` / kill path
        ;; switches CR3 to kernel_idle_pd first.  Kernel-half PDEs
        ;; are left alone; the kernel-half PTs they reference are
        ;; shared and outlive every per-program PD.
        push eax
        push ebx
        push ecx
        push edx
        push edi
        push esi
        mov esi, eax                            ; ESI = pd_phys (saved for the final free)
        call kmap_map                           ; EAX = pd_kvirt
        mov edi, eax                            ; EDI = pd_kvirt; PT walks below preserve EDI
        xor ebx, ebx                            ; EBX = PDE index
.pde_loop:
        mov eax, [edi + ebx*4]
        test eax, ADDRESS_SPACE_PDE_PRESENT
        jz .next_pde
        ;; PDE present: kmap the PT, walk its PTEs.
        and eax, 0xFFFFF000                     ; EAX = PT phys
        push eax                                ; remember for the final frame_free
        call kmap_map                           ; EAX = PT kvirt
        mov edx, eax                            ; EDX = PT kvirt; preserved across kmap_/frame_free calls
        xor ecx, ecx                            ; ECX = PTE index
.pte_loop:
        mov eax, [edx + ecx*4]
        test eax, ADDRESS_SPACE_PDE_PRESENT
        jz .next_pte
        test eax, ADDRESS_SPACE_PTE_SHARED      ; shared frame (vDSO)?
        jnz .next_pte                           ; yes — leave it for other PDs
        and eax, 0xFFFFF000                     ; user-page phys
        call frame_free
.next_pte:
        inc ecx
        cmp ecx, 1024
        jb .pte_loop
        ;; Release the PT mapping, then free the PT frame.
        mov eax, edx
        call kmap_unmap
        pop eax                                 ; PT phys (saved at .pde_loop)
        call frame_free
.next_pde:
        inc ebx
        cmp ebx, ADDRESS_SPACE_USER_PDE_COUNT
        jb .pde_loop
        ;; Release the PD mapping, then free the PD frame.
        mov eax, edi
        call kmap_unmap
        mov eax, esi
        call frame_free
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

address_space_map_page:
        ;; EAX = pd_phys, EBX = user_virt, ECX = phys, EDX = flags.
        ;; Inserts a PTE.  Allocates a PT frame on demand if the PDE is
        ;; not-present.  Does not invalidate TLB — caller invalidates if
        ;; pd_phys == current CR3 (the typical caller is `program_enter`
        ;; building a PD that's not yet installed, so the invlpg is
        ;; unnecessary).  Output: CF clear on success; CF set on OOM
        ;; or out-of-range user_virt (with no PTE installed and no PT
        ;; leaked).
        ;;
        ;; user_virt must be in the user half ([0, KERNEL_VIRT_BASE)).
        ;; A user_virt at or above KERNEL_VIRT_BASE would land on PDE
        ;; >= FIRST_KERNEL_PDE (768) — those PDEs are copy-imaged from
        ;; `kernel_idle_pd` and point at PTs shared by every PD; the
        ;; "PDE present" branch below would then write a user-RW PTE
        ;; into the shared kernel PT and corrupt every program's
        ;; kernel direct map.  Reject with CF=1 before any side
        ;; effects so the caller's OOM-recovery path can tear down
        ;; the partial PD without touching kernel-half mappings.
        cmp ebx, KERNEL_VIRT_BASE
        jae .out_of_range
        push eax                                ; saved EAX = pd_phys
        push ebx                                ; saved EBX = user_virt
        push ecx                                ; saved ECX = phys
        push edx                                ; saved EDX = flags
        push edi
        push esi
        ;; ESI = PDE index = user_virt >> 22.
        mov esi, ebx
        shr esi, 22
        ;; Map the PD via kmap.  Pushed pd_kvirt stays on the stack
        ;; until the final unmap; all subsequent reads of the
        ;; saved-prologue values shift by +4 to compensate.
        call kmap_map                           ; EAX = pd_kvirt
        push eax                                ; [esp+0] = pd_kvirt
        mov edi, eax
        mov eax, [edi + esi*4]
        test eax, ADDRESS_SPACE_PDE_PRESENT
        jnz .pde_present
        ;; PDE not present: allocate a fresh PT, zero it, install the PDE.
        call frame_alloc
        jc .oom_pd_mapped
        push eax                                ; remember PT phys for the PDE install
        call kmap_map                           ; EAX = PT kvirt
        push eax                                ; remember PT kvirt for the unmap
        mov edi, eax
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop eax                                 ; PT kvirt
        call kmap_unmap                         ; release the zero-fill mapping
        pop eax                                 ; PT phys
        ;; Install PDE = pt_phys | P | RW | U.  EDI was clobbered by
        ;; the rep stosd; reload pd_kvirt from [esp].
        mov edi, [esp]                          ; pd_kvirt
        mov edx, eax
        or edx, ADDRESS_SPACE_PDE_USER_RW
        mov [edi + esi*4], edx
        ;; EAX still holds the PT phys; jump past the .pde_present
        ;; PT-phys extract since we already have it.
        jmp .pt_write
.pde_present:
        ;; EAX = PDE value; mask off flag bits to get PT phys.
        and eax, 0xFFFFF000
.pt_write:
        ;; EAX = PT phys; map it for the PTE write.
        call kmap_map                           ; EAX = PT kvirt
        push eax                                ; [esp+0] = PT kvirt; pd_kvirt at [esp+4]
        ;; After the PT-kvirt push and the pd-kvirt push, original
        ;; saved EBX (user_virt) is at [esp + 24], saved ECX (phys)
        ;; at [esp + 20], saved EDX (flags) at [esp + 16].
        mov ebx, [esp + 24]                     ; user_virt
        shr ebx, 12
        and ebx, 0x3FF
        mov ecx, [esp + 20]                     ; phys
        and ecx, 0xFFFFF000
        or ecx, [esp + 16]                      ; flags
        mov [eax + ebx*4], ecx
        pop eax                                 ; PT kvirt
        call kmap_unmap
        pop eax                                 ; pd_kvirt
        call kmap_unmap
        clc
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret
.oom_pd_mapped:
        ;; frame_alloc failed after the PD was kmap'd.  Release the
        ;; PD mapping before unwinding so the kmap slot doesn't leak.
        pop eax                                 ; pd_kvirt
        call kmap_unmap
        stc
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret
.out_of_range:
        ;; user_virt is in the kernel half — refuse before touching
        ;; the PD.  No prologue saves to unwind.
        stc
        ret

address_space_unmap_page:
        ;; EAX = pd_phys, EBX = user_virt.  Clears the PTE if present;
        ;; no-op if the PDE or PTE was already not-present.  Does not
        ;; free the underlying frame — caller's responsibility.  Issues
        ;; `invlpg` if pd_phys == current CR3 so the just-cleared
        ;; mapping doesn't linger in the TLB.  PD and PT are both
        ;; reached through kmap so high-physical PD/PT frames stay
        ;; addressable.
        push eax
        push ebx
        push ecx
        push edx
        push edi
        push esi
        mov esi, ebx
        shr esi, 22                             ; ESI = PDE index
        call kmap_map                           ; EAX = pd_kvirt (input was pd_phys)
        push eax                                ; [esp+0] = pd_kvirt
        mov ecx, [eax + esi*4]
        test ecx, ADDRESS_SPACE_PDE_PRESENT
        jz .release_pd
        ;; PDE present: kmap the PT, clear the PTE.
        and ecx, 0xFFFFF000                     ; ECX = PT phys
        mov eax, ecx
        call kmap_map                           ; EAX = PT kvirt
        mov edx, eax                            ; EDX = PT kvirt
        mov edi, ebx
        shr edi, 12
        and edi, 0x3FF                          ; EDI = PTE index
        mov dword [edx + edi*4], 0
        ;; Release the PT mapping.
        mov eax, edx
        call kmap_unmap
        ;; Invalidate the TLB if this PD is the live one.  After the
        ;; pd_kvirt push the prologue's saved EAX (pd_phys) is at
        ;; [esp + 24] (6 prologue dwords + the pd_kvirt push).
        mov ecx, cr3
        cmp ecx, [esp + 24]
        jne .release_pd
        invlpg [ebx]
.release_pd:
        pop eax                                 ; pd_kvirt
        call kmap_unmap
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret
