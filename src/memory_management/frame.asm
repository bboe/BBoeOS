;;; ------------------------------------------------------------------------
;;; memory_management/frame.asm — physical-frame bitmap allocator.
;;;
;;; Tracks free vs in-use 4 KB physical frames in a static bitmap sized
;;; for a 256 MB ceiling (8192 bytes, one bit per frame).  RAM beyond
;;; the ceiling is ignored.  All entry points run at CPL=0.
;;;
;;; Public:
;;;   frame_init(esi = e820 list)        scan E820, mark free regions
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
;;; ------------------------------------------------------------------------

;; FRAME_BITMAP_BITS / FRAME_BITMAP_BYTES size the bitmap for a 256 MB
;; ceiling.  The storage itself lives at FRAME_BITMAP_PHYS in the
;; post-kernel reserved cluster (see kernel.asm) — extracting the
;; bitmap from kernel.bin saves 8 KB of zero bytes on disk.  See
;; project_frame_bitmap_dynamic_e820 in memory for the future move
;; to E820-sized runtime allocation that 4 GB support will need.
%define FRAME_BITMAP_BITS       (256 * 1024 * 1024 / 4096)
%define FRAME_BITMAP_BYTES      (FRAME_BITMAP_BITS / 8)

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
        cmp eax, FRAME_BITMAP_BITS
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
        cmp edi, frame_bitmap + FRAME_BITMAP_BYTES
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
        cmp eax, FRAME_BITMAP_BITS
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
        ;; terminates).  Marks every type=1 region as free, leaving the
        ;; rest reserved.  Caller follows up with frame_reserve_range
        ;; for kernel-image / boot-PD / first-PT carve-outs.
        push eax
        push ebx
        push ecx
        push edx
        push edi
        ;; Start from all-reserved.
        mov edi, frame_bitmap
        mov ecx, FRAME_BITMAP_BYTES / 4
        mov eax, 0xFFFFFFFF
        cld
        rep stosd
        mov dword [frame_free_count], 0
.entry_loop:
        mov eax, [esi + 0]                      ; base low
        mov ebx, [esi + 8]                      ; length low
        mov edx, [esi + 16]                     ; type
        ;; Zero-length terminator: low and high length both zero.
        test ebx, ebx
        jnz .got_entry
        cmp dword [esi + 12], 0
        je .done
.got_entry:
        cmp edx, 1                              ; type 1 = usable RAM
        jne .skip
        ;; Skip entries above the 4 GB low-base ceiling — bases above
        ;; 4 GB live in [esi+4] and we don't model them in the bitmap.
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
        pop edi
        pop edx
        pop ecx
        pop ebx
        pop eax
        ret

frame_mark_range_free:
        ;; EAX = base, EBX = length.  Marks complete frames within the
        ;; range as free; partial frames at either end are skipped.
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
        cmp edx, FRAME_BITMAP_BITS
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
        cmp eax, [frame_max_phys]
        jbe .skip
        mov [frame_max_phys], eax
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
        cmp edx, FRAME_BITMAP_BITS
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
        ;; the kernel direct map.  Keeping the 8 KB of zero-init storage
        ;; outside kernel.bin trims the on-disk image.  frame_init
        ;; populates the bitmap before any frame_alloc / frame_free
        ;; runs, so the on-disk garbage there doesn't matter.
        align 4
frame_free_count:
        dd 0
frame_search_hint:
        dd 0
frame_total:
        dd 0
frame_max_phys:
        dd 0
