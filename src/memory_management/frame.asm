;;; ------------------------------------------------------------------------
;;; memory_management/frame.asm — physical-frame bitmap allocator.
;;;
;;; Tracks free vs in-use 4 KB physical frames in a bitmap whose size is
;;; chosen at boot from the highest E820 base seen.  RAM beyond the
;;; clamp ceiling (LAST_KERNEL_PDE worth of direct-map coverage) is
;;; ignored.  All entry points run at CPL=0.
;;;
;;; Public:
;;;   frame_init(esi = e820 list)        scan E820, size & init bitmap,
;;;                                       mark free regions
;;;   frame_alloc()         -> EAX = phys, CF on OOM (CF clear on hit)
;;;   frame_free(eax = phys)             clear the bit
;;;   frame_reserve_range(eax = base, ecx = length)
;;;                                       mark range as in-use post-init
;;;
;;; A free bit = 0; allocation sets the bit.  First-fit scan starting
;;; from frame_search_hint, which advances on alloc and rewinds on free
;;; to keep the working set clustered.
;;;
;;; Bitmap byte layout: dword[k] holds frame numbers 32k .. 32k+31, with
;;; bit (frame & 31) inside the dword.  All bit twiddling honors x86's
;;; shift-count masking — `shl ebx, cl` masks CL to 5 bits, so we can
;;; pass the raw frame number in CL as long as we mask the byte offset
;;; separately.
;;;
;;; The bitmap lives at the fixed kernel-virt address `frame_bitmap`
;;; (= DIRECT_MAP_BASE + FRAME_BITMAP_PHYS, in the post-kernel cluster
;;; — see kernel.asm).  Its byte length is `[frame_bitmap_bytes]`,
;;; populated by frame_init from the highest type=1 E820 entry,
;;; clamped so the bitmap never describes frames above the kernel
;;; direct map's reach (LAST_KERNEL_PDE in kernel.asm).  See
;;; project_frame_bitmap_dynamic_e820 in memory for the design notes.
;;; ------------------------------------------------------------------------

;; Direct-map ceiling: the kernel can only reach phys < (LAST_KERNEL_PDE
;; - FIRST_KERNEL_PDE) * 4 MB through the kernel direct map.  frame_init
;; clamps `frame_max_phys` to this ceiling so the bitmap never describes
;; frames the kernel has no virtual address for.  RAM above this is
;; silently ignored (kmap window territory — phase 2).
%define FRAME_DIRECT_MAP_LIMIT  ((LAST_KERNEL_PDE - FIRST_KERNEL_PDE) * 0x400000)

frame_alloc:
        ;; First-fit scan from frame_search_hint.  Returns EAX = phys
        ;; of allocated frame, CF clear; CF set on OOM.  Caller zeroes
        ;; the page if it needs zeroed memory — frame_alloc only
        ;; reserves the bit.
        push ebx
        push ecx
        push edi
        mov ecx, [frame_search_hint]            ; ECX = starting frame number
        mov edi, frame_bitmap
        mov ebx, ecx
        shr ebx, 5                              ; dword index from start
        shl ebx, 2                              ; byte offset of containing dword
        add edi, ebx                            ; EDI = bitmap dword pointer
        and ecx, 31                             ; bit position within first dword
.scan_dword:
        mov eax, [edi]
        not eax                                 ; invert: 1 = free
        bsf eax, eax                            ; lowest free bit (0..31); ZF=1 if all bits clear
        jz .next_dword                          ; nothing free in this dword
        cmp eax, ecx
        jb .next_dword                          ; below the hint within this dword
        ;; Found a free bit.  Frame number = ((edi - frame_bitmap) * 8) + bit.
        mov ebx, edi
        sub ebx, frame_bitmap
        shl ebx, 3                              ; bytes -> bits
        add eax, ebx                            ; EAX = absolute frame number
        cmp eax, [frame_bitmap_bits]
        jae .oom
        ;; Mark the bit set.  CL = bit position (low 5 bits used by SHL).
        mov ecx, eax
        mov ebx, 1
        shl ebx, cl                             ; EBX = mask
        mov ecx, eax
        shr ecx, 5                              ; dword index
        or [frame_bitmap + ecx*4], ebx
        ;; Advance the hint past the just-allocated frame and decrement
        ;; the running free count.
        mov ecx, eax
        inc ecx
        mov [frame_search_hint], ecx
        dec dword [frame_free_count]
        ;; Convert frame number to physical address and return.
        shl eax, 12
        clc
        pop edi
        pop ecx
        pop ebx
        ret
.next_dword:
        add edi, 4
        xor ecx, ecx                            ; only the first dword respects the hint
        mov ebx, frame_bitmap
        add ebx, [frame_bitmap_bytes]
        cmp edi, ebx
        jb .scan_dword
.oom:
        stc
        pop edi
        pop ecx
        pop ebx
        ret

frame_free:
        ;; EAX = phys.  Clears the bit.  Pulls frame_search_hint back if
        ;; the freed frame is below the current hint so a subsequent
        ;; alloc can reclaim it.  Out-of-range frees are silent no-ops.
        push ebx
        push ecx
        shr eax, 12                             ; frame number
        cmp eax, [frame_bitmap_bits]
        jae .done
        mov ecx, eax
        mov ebx, 1
        shl ebx, cl                             ; mask (CL low 5 bits)
        not ebx                                 ; mask of bits to keep
        mov ecx, eax
        shr ecx, 5                              ; dword index
        and [frame_bitmap + ecx*4], ebx
        inc dword [frame_free_count]
        cmp eax, [frame_search_hint]
        jae .done
        mov [frame_search_hint], eax
.done:
        pop ecx
        pop ebx
        ret

frame_init:
        ;; ESI = pointer to the E820 list (24-byte entries; zero entry
        ;; terminates).  Two passes:
        ;;
        ;;   1. Find the highest type=1 frame base across the table.
        ;;      Clamp to FRAME_DIRECT_MAP_LIMIT (RAM beyond the kernel
        ;;      direct map is unreachable and stays untracked).
        ;;   2. Size the bitmap from that ceiling, fill all-1s, then
        ;;      walk the table again marking every type=1 region as
        ;;      free.
        ;;
        ;; Caller follows up with frame_reserve_range for kernel-image
        ;; / boot-PD / first-PT / bitmap carve-outs.
        push eax
        push ebx
        push ecx
        push edx
        push edi
        push esi

        ;; --- Pass 1: find the highest type=1 frame base ---
        ;; ESI was just pushed and points at the E820 list head; we
        ;; advance it during the scan and rewind from [esp] for pass 2.
        mov dword [frame_max_phys], 0
.scan_loop:
        mov eax, [esi + 0]                      ; base low
        mov ebx, [esi + 8]                      ; length low
        mov edx, [esi + 16]                     ; type
        ;; Zero-length terminator: low and high length both zero.
        test ebx, ebx
        jnz .scan_check
        cmp dword [esi + 12], 0
        je .scan_done
.scan_check:
        cmp edx, 1                              ; type 1 = usable RAM
        jne .scan_next
        ;; Skip entries whose base is above the 4 GB low-base ceiling.
        cmp dword [esi + 4], 0
        jne .scan_next
        ;; Highest frame base fully contained in [base, base+length):
        ;;   round_up_base   = (base + 0xFFF) & ~0xFFF
        ;;   round_down_end  = (base + length) & ~0xFFF
        ;; If round_up_base >= round_down_end the entry holds no full
        ;; frame (e.g. sub-frame BIOS slivers) and we ignore it; else
        ;; the highest fully-contained frame base is round_down_end -
        ;; 0x1000.
        mov ecx, eax
        add ecx, 0xFFF
        and ecx, ~0xFFF                         ; ECX = round_up_base
        mov edx, eax
        add edx, ebx
        and edx, ~0xFFF                         ; EDX = round_down_end
        cmp ecx, edx
        jae .scan_next                          ; no full frame in this entry
        sub edx, 0x1000                         ; EDX = highest frame base
        cmp edx, [frame_max_phys]
        jbe .scan_next
        mov [frame_max_phys], edx
.scan_next:
        add esi, 24
        jmp .scan_loop
.scan_done:
        ;; Clamp frame_max_phys to the direct-map ceiling.  Frames
        ;; above this are unreachable through the kernel direct map.
        mov eax, [frame_max_phys]
        cmp eax, FRAME_DIRECT_MAP_LIMIT
        jb .clamp_ok
        mov eax, FRAME_DIRECT_MAP_LIMIT - 0x1000
        mov [frame_max_phys], eax
.clamp_ok:

        ;; --- Size the bitmap and fill all-1s ---
        ;;
        ;; total_frames = (frame_max_phys >> 12) + 1
        ;; dword_count  = (total_frames + 31) / 32          (round up)
        ;; bitmap_bytes = dword_count * 4
        ;; bitmap_bits  = dword_count * 32
        mov eax, [frame_max_phys]
        shr eax, 12                             ; highest frame number
        inc eax                                 ; total frames
        add eax, 31
        shr eax, 5                              ; dword count
        mov ecx, eax
        shl ecx, 2
        mov [frame_bitmap_bytes], ecx
        shl eax, 5
        mov [frame_bitmap_bits], eax

        mov edi, frame_bitmap
        mov ecx, [frame_bitmap_bytes]
        shr ecx, 2                              ; dword count
        mov eax, 0xFFFFFFFF
        cld
        rep stosd
        mov dword [frame_free_count], 0

        ;; --- Pass 2: mark every type=1 region as free ---
        mov esi, [esp]                          ; ESI = E820 list head
.entry_loop:
        mov eax, [esi + 0]                      ; base low
        mov ebx, [esi + 8]                      ; length low
        mov edx, [esi + 16]                     ; type
        test ebx, ebx
        jnz .got_entry
        cmp dword [esi + 12], 0
        je .done
.got_entry:
        cmp edx, 1                              ; type 1 = usable RAM
        jne .skip
        cmp dword [esi + 4], 0
        jne .skip
        call frame_mark_range_free              ; (eax = base, ebx = length)
.skip:
        add esi, 24
        jmp .entry_loop
.done:
        ;; Snapshot total = current free count.  frame_reserve_range
        ;; calls in the boot path will tick frame_free_count down without
        ;; touching frame_total.
        mov eax, [frame_free_count]
        mov [frame_total], eax
        mov dword [frame_search_hint], 0
        pop esi
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

frame_mark_range_free:
        ;; EAX = base, EBX = length.  Marks complete frames within the
        ;; range as free; partial frames at either end are skipped.
        ;; Out-of-range frames (above the bitmap clamp) are silently
        ;; dropped.
        push eax
        push ebx
        push ecx
        push edx
        push esi
        mov esi, eax                            ; preserve original base
        ;; Round base up to frame boundary.
        add eax, 0xFFF
        and eax, ~0xFFF
        ;; End = original base + length, rounded down to frame boundary.
        mov ecx, esi
        add ecx, ebx
        and ecx, ~0xFFF
        cmp eax, ecx
        jae .done
.loop:
        mov edx, eax
        shr edx, 12                             ; frame number
        cmp edx, [frame_bitmap_bits]
        jae .skip
        mov ebx, 1
        push ecx
        mov ecx, edx
        shl ebx, cl                             ; EBX = single-bit mask
        pop ecx
        push ecx
        mov ecx, edx
        shr ecx, 5                              ; ECX = dword index
        ;; Test against the single-bit mask: non-zero means the frame
        ;; was previously reserved.  Only inc free count on the
        ;; transition from reserved → free.
        test [frame_bitmap + ecx*4], ebx
        jz .already_free                        ; bit already 0 → free
        not ebx                                 ; EBX = NOT mask (clear-the-bit form)
        and [frame_bitmap + ecx*4], ebx
        inc dword [frame_free_count]
.already_free:
        pop ecx
.skip:
        add eax, 0x1000
        cmp eax, ecx
        jb .loop
.done:
        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

frame_reserve_range:
        ;; EAX = base, ECX = length.  Marks complete frames within the
        ;; range as in-use; partial frames at either end are rounded
        ;; outward (a partial reservation reserves the whole frame so
        ;; the allocator can't hand it out).
        push eax
        push ebx
        push ecx
        push edx
        push esi
        mov esi, eax                            ; preserve original base
        ;; End = base + length, rounded up to a frame boundary.
        add ecx, eax
        add ecx, 0xFFF
        and ecx, ~0xFFF
        ;; Base rounded down to a frame boundary.
        and eax, ~0xFFF
        cmp eax, ecx
        jae .done
.loop:
        mov edx, eax
        shr edx, 12                             ; frame number
        cmp edx, [frame_bitmap_bits]
        jae .skip
        mov ebx, 1
        push ecx
        mov ecx, edx
        shl ebx, cl                             ; mask
        pop ecx
        push ecx
        mov ecx, edx
        shr ecx, 5                              ; dword index
        ;; Only decrement frame_free_count if the bit was previously clear.
        test [frame_bitmap + ecx*4], ebx
        jnz .already_set
        or [frame_bitmap + ecx*4], ebx
        dec dword [frame_free_count]
        jmp .next
.already_set:
.next:
        pop ecx
.skip:
        add eax, 0x1000
        cmp eax, ecx
        jb .loop
.done:
        pop esi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

        ;; frame_bitmap storage lives at FRAME_BITMAP_PHYS in the
        ;; post-kernel reserved cluster (kernel.asm), reached through
        ;; the kernel direct map.  Keeping the storage outside
        ;; kernel.bin trims the on-disk image; the underlying frames
        ;; are reserved at boot via the LOW_RESERVE-region sweep, which
        ;; uses the same dynamic bitmap_bytes value computed by
        ;; frame_init.
        align 4
frame_bitmap_bits:
        dd 0
frame_bitmap_bytes:
        dd 0
frame_free_count:
        dd 0
frame_max_phys:
        dd 0
frame_search_hint:
        dd 0
frame_total:
        dd 0
