;;; ------------------------------------------------------------------------
;;; memory_management/address_space.asm — per-program address-space helpers.
;;;
;;; Builds and tears down per-program user page directories.  Kernel-half
;;; PDEs (768..1023, the 256 MB direct map at virtual
;;; 0xC0000000..0xCFFFFFFF) are copied verbatim from `kernel_pd_template`
;;; at address_space_create time and never modified afterward — that
;;; invariant is what lets us avoid fan-out updates when the kernel
;;; installs a new kernel-half mapping.
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
        ;; Allocate one frame, zero it, then copy the top-256 PDEs from
        ;; `kernel_pd_template` into the new PD's kernel-half slot.
        ;; Output: EAX = pd_phys, CF clear on success; CF set on OOM.
        push ebx
        push ecx
        push edi
        push esi
        call frame_alloc
        jc .oom
        ;; Zero all 1024 PDEs via the direct map.
        push eax                                ; save pd_phys for return
        mov edi, eax
        add edi, ADDRESS_SPACE_DIRECT_MAP_BASE
        push edi                                ; remember PD direct-map base
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        ;; Copy top-256 PDEs from kernel_pd_template into PDE[768..1023].
        ;; ESI = source kernel-virt, EDI = destination kernel-virt, both
        ;; offset by ADDRESS_SPACE_USER_PDE_COUNT * 4 bytes to skip the
        ;; user half.
        mov esi, [kernel_pd_template_phys]
        add esi, ADDRESS_SPACE_DIRECT_MAP_BASE
        add esi, ADDRESS_SPACE_USER_PDE_COUNT * 4
        pop edi                                 ; PD direct-map base
        add edi, ADDRESS_SPACE_USER_PDE_COUNT * 4
        mov ecx, ADDRESS_SPACE_KERNEL_PDE_COUNT
        rep movsd
        pop eax                                 ; restore pd_phys
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
        ;; EAX = pd_phys.  Walks PDEs 0..767 (user half).  For each
        ;; present PDE, walks the PT, frees every present user-page
        ;; frame, then frees the PT frame itself.  PTEs with the
        ;; `ADDRESS_SPACE_PTE_SHARED` AVL bit set (vDSO code page) are
        ;; skipped — those frames live in shared tables managed by the
        ;; kernel and outlive any one address space.  Finally frees the PD frame.  Caller must not have
        ;; pd_phys loaded in CR3 — the `sys_exit` / kill path switches
        ;; CR3 to kernel_pd_template first.  Kernel-half PDEs are left
        ;; alone; the kernel-half PTs they reference are shared and
        ;; outlive every per-program PD.
        push eax
        push ebx
        push ecx
        push edx
        push edi
        push esi
        mov esi, eax                            ; ESI = pd_phys (saved)
        mov edi, eax
        add edi, ADDRESS_SPACE_DIRECT_MAP_BASE  ; EDI = PD direct-map address
        xor ebx, ebx                            ; EBX = PDE index
.pde_loop:
        mov eax, [edi + ebx*4]
        test eax, ADDRESS_SPACE_PDE_PRESENT
        jz .next_pde
        ;; PDE present: walk the PT.
        mov edx, eax
        and edx, 0xFFFFF000                     ; EDX = PT phys
        push edx                                ; save for the PT-frame free
        add edx, ADDRESS_SPACE_DIRECT_MAP_BASE  ; EDX = PT direct-map address
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
        ;; Free the PT frame itself.
        pop eax                                 ; PT phys (saved at .pde_loop)
        call frame_free
.next_pde:
        inc ebx
        cmp ebx, ADDRESS_SPACE_USER_PDE_COUNT
        jb .pde_loop
        ;; Free the PD frame.
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
        ;; (with no PTE installed and no PT leaked).
        push eax
        push ebx
        push ecx
        push edx
        push edi
        push esi
        ;; ESI = PDE index = user_virt >> 22.
        mov esi, ebx
        shr esi, 22
        ;; EDI = PD direct-map address.
        mov edi, eax
        add edi, ADDRESS_SPACE_DIRECT_MAP_BASE
        mov eax, [edi + esi*4]
        test eax, ADDRESS_SPACE_PDE_PRESENT
        jnz .pde_present
        ;; PDE not present: allocate a fresh PT, zero it, install the PDE.
        call frame_alloc
        jc .oom
        push eax                                ; save PT phys
        mov edi, eax
        add edi, ADDRESS_SPACE_DIRECT_MAP_BASE  ; EDI = PT direct-map address
        mov ecx, 1024
        xor eax, eax
        cld
        rep stosd
        pop eax                                 ; restore PT phys
        ;; Install PDE = pt_phys | P | RW | U.  Reload EDI from the
        ;; PD-direct-map base saved on the prologue stack ([esp+20] is
        ;; the saved EAX = pd_phys).
        mov edi, [esp + 20]                     ; saved EAX = pd_phys
        add edi, ADDRESS_SPACE_DIRECT_MAP_BASE
        mov edx, eax
        or edx, ADDRESS_SPACE_PDE_USER_RW
        mov [edi + esi*4], edx
.pde_present:
        ;; EAX = PDE value; mask off flag bits to get PT phys.
        and eax, 0xFFFFF000
        add eax, ADDRESS_SPACE_DIRECT_MAP_BASE  ; EAX = PT direct-map address
        ;; PTE index = (user_virt >> 12) & 0x3FF.
        mov ebx, [esp + 16]                     ; saved EBX = user_virt
        shr ebx, 12
        and ebx, 0x3FF
        ;; Build PTE = (phys & ~0xFFF) | flags.
        mov ecx, [esp + 12]                     ; saved ECX = phys
        and ecx, 0xFFFFF000
        or ecx, [esp + 8]                       ; saved EDX = flags
        mov [eax + ebx*4], ecx
        clc
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret
.oom:
        ;; frame_alloc failed before we modified the PD.  Just unwind
        ;; the prologue.
        stc
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

address_space_unmap_page:
        ;; EAX = pd_phys, EBX = user_virt.  Clears the PTE if present;
        ;; no-op if the PDE or PTE was already not-present.  Does not
        ;; free the underlying frame — caller's responsibility.  Issues
        ;; `invlpg` if pd_phys == current CR3 so the just-cleared
        ;; mapping doesn't linger in the TLB.
        push eax
        push ebx
        push ecx
        push edi
        push esi
        ;; ESI = PDE index, EDI = PD direct-map address.
        mov esi, ebx
        shr esi, 22
        mov edi, eax
        add edi, ADDRESS_SPACE_DIRECT_MAP_BASE
        mov ecx, [edi + esi*4]
        test ecx, ADDRESS_SPACE_PDE_PRESENT
        jz .done
        ;; ECX = PT direct-map address; EDI = PTE index.
        and ecx, 0xFFFFF000
        add ecx, ADDRESS_SPACE_DIRECT_MAP_BASE
        mov edi, ebx
        shr edi, 12
        and edi, 0x3FF
        mov dword [ecx + edi*4], 0
        ;; Invalidate the TLB if this PD is the live one.
        mov ecx, cr3
        cmp ecx, eax
        jne .done
        invlpg [ebx]
.done:
        pop esi
        pop edi
        pop ecx
        pop ebx
        pop eax
        ret
