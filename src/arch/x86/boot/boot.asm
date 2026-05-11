;;; ------------------------------------------------------------------------
;;; boot.asm — pre-paging boot binary.
;;;
;;; Layout (org 0x7C00):
;;;   [0x7C00..0x7DFF]  MBR — disk reset + INT 13h read of post-MBR
;;;                      boot bytes; jumps into the post-MBR region
;;;                      once those sectors are resident.
;;;   [0x7E00..]        Post-MBR real-mode bootstrap: second INT 13h
;;;                      read pulls kernel.bin into physical 0x20000
;;;                      (its final home — no later relocation copy),
;;;                      then VGA font copy / E820 probe / PIC remap /
;;;                      A20 / GDT / CR0.PE flip / far-jump into the
;;;                      32-bit `early_pe_entry`.  Padded to a fixed
;;;                      BOOT_SECTORS so the build script only has to
;;;                      measure kernel.bin's sector count.
;;;
;;; After the PE flip, `early_pe_entry` runs in flat 32-bit at low
;;; physical, builds the boot PD + first kernel PT, enables paging,
;;; and far-jumps to `high_entry` at the kernel-virt entry point
;;; (HIGH_ENTRY_VIRT = 0xFF820000 — the very first byte of
;;; kernel.bin per its `org` directive, which equals KERNEL_VIRT_BASE
;;; + KERNEL_LOAD_PHYS so the kernel runs in the direct map without
;;; needing a separate higher-half mapping).
;;;
;;; boot.asm intentionally has no IDT.  An exception during early-PE
;;; bootstrap triple-faults; the bootstrap is short and tested.  The
;;; high-half kernel installs its own IDT first thing.
;;;
;;; GDT layout (matches kernel.asm's view; the kernel re-lgdts its own
;;; copy after paging is on so it never depends on boot.asm's tables
;;; living in low physical memory):
;;;   0x00 null
;;;   0x08 code: base=0, limit=4 GB, 32-bit, DPL=0, exec/read
;;;   0x10 data: base=0, limit=4 GB, 32-bit, DPL=0, read/write
;;;   0x18 code: base=0, limit=4 GB, 32-bit, DPL=3, exec/read
;;;   0x20 data: base=0, limit=4 GB, 32-bit, DPL=3, read/write
;;;   0x28 TSS:  base patched at runtime by kernel.asm
;;; ------------------------------------------------------------------------

        org 7C00h
        %include "constants.asm"

        ;; KERNEL_SECTORS is the number of 512-byte sectors of
        ;; kernel.bin on disk.  It's passed via `nasm -DKERNEL_SECTORS=N`
        ;; from make_os.sh (computed from kernel.bin's size after the
        ;; first build pass).  BOOT_SECTORS is the number of post-MBR
        ;; sectors of boot.bin itself; it's derived from the post-MBR
        ;; region's size at the bottom of this file via an `equ` whose
        ;; forward reference NASM resolves in a later pass — every use
        ;; below is a fixed-size operand (imm8 or `dw`) so the value
        ;; can change without rippling instruction widths.

        ICW1_INIT       equ 11h
        ICW4_8086       equ 01h
        PIC1_CASCADE    equ 04h
        PIC1_VECTOR     equ 20h         ; master IRQ 0..7 → 0x20..0x27
        PIC2_CASCADE_ID equ 02h
        PIC2_VECTOR     equ 28h         ; slave  IRQ 8..15 → 0x28..0x2F
        PIC_MASK_ALL    equ 0FFh

        ;; Flat 32-bit code / data selectors used by both boot.asm and
        ;; kernel.asm.  Match the GDT layout below.
        BOOT_CODE_SELECTOR      equ 08h
        BOOT_DATA_SELECTOR      equ 10h

        ;; Physical addresses for early-PE page-table setup.  Boot PD and
        ;; first kernel PT are positioned immediately above kernel.bin by
        ;; make_os.sh, which computes KERNEL_RESERVED_BASE from the measured
        ;; kernel.bin size and passes it as -DKERNEL_RESERVED_BASE=N.
        ;; The fallback keeps direct nasm invocations buildable.
        %ifndef KERNEL_RESERVED_BASE
        %define KERNEL_RESERVED_BASE 0x180000
        %endif
        ;; Kernel-side layout mirror.  Must match the equ chain in
        ;; src/arch/x86/kernel.asm so that boot.asm's BOOT_PD_PHYS and
        ;; FIRST_KERNEL_PT_PHYS resolve to the same physical addresses
        ;; the kernel already expects.  In-memory layout (low to high):
        ;;   KERNEL_RESERVED_BASE          (kernel stack)
        ;;     + KERNEL_STACK_BYTES            (4 KB)
        ;;   BOOT_PD_PHYS                      (4 KB)
        ;;   FIRST_KERNEL_PT_PHYS              (4 KB)
        ;;
        ;; kernel.bin loads directly to its final physical home — there's
        ;; no real-mode-to-PE relocation copy.  KERNEL_LOAD_PHYS sits
        ;; above the vDSO target frame at phys 0x10000 and below the VGA
        ;; aperture at phys 0xA0000, so the entire reserved region fits
        ;; in conventional memory and the OS boots under QEMU `-m 1`.
        ;;
        ;; BOOT_DISK_PHYS / DIRECTORY_SECTOR_PHYS are the embedded boot
        ;; stash inside kernel.bin (offset BOOT_STASH_OFFSET): a 1-byte
        ;; boot_disk slot followed by a 2-byte directory_sector slot.
        ;; boot.asm writes both AFTER the kernel.bin INT 13h read so the
        ;; load doesn't clobber them.  HIGH_ENTRY_VIRT is the kernel
        ;; far-jump target (= KERNEL_VIRT_BASE + KERNEL_LOAD_PHYS, so
        ;; the kernel runs at its direct-map alias).
        BOOT_DISK_PHYS              equ KERNEL_LOAD_PHYS + BOOT_STASH_OFFSET
        BOOT_PD_PHYS                equ KERNEL_RESERVED_BASE + KERNEL_STACK_BYTES
        DIRECTORY_SECTOR_PHYS       equ KERNEL_LOAD_PHYS + BOOT_STASH_OFFSET + 1
        FIRST_KERNEL_PDE            equ 1022                ; KERNEL_VIRT_BASE / 0x400000; must equal kernel.asm's value
        FIRST_KERNEL_PT_PHYS        equ BOOT_PD_PHYS + 0x1000
        HIGH_ENTRY_VIRT             equ 0xFF820000          ; KERNEL_VIRT_BASE + KERNEL_LOAD_PHYS
        KERNEL_LOAD_PHYS            equ 0x20000
        KERNEL_STACK_BYTES          equ 0x1000

start:
        xor ax, ax
        mov ds, ax
        mov es, ax

        ;; Dedicated stack at SS=0x9000, SP=0xFFF0 (linear 0x90000-0x9FFF0)
        ;; owns its entire segment so it can never collide with the
        ;; kernel image at 0x7C00 / 0x20000 or the post-MBR boot bytes.
        cli
        mov ax, 9000h
        mov ss, ax
        mov sp, 0FFF0h
        sti

        ;; Save the BIOS drive number in BP for the rest of the
        ;; real-mode bootstrap.  Avoids burning a permanent low-memory
        ;; reservation for a single byte; the saved value is written
        ;; into kernel.bin's embedded boot_disk slot once the kernel
        ;; image is loaded.  BP is otherwise unused by the BIOS calls
        ;; below.
        mov bp, dx                      ; BPL = drive number

        ;; Reset disk controllers before the first read; defensive on
        ;; real hardware, no-op on QEMU.
        xor ax, ax
        int 13h
        jc .error

        ;; Read the post-MBR sectors of boot.bin (BOOT_SECTORS) into
        ;; linear 0x7E00.  CHS sector numbers are 1-based; the MBR is
        ;; sector 1, so the post-MBR data starts at sector 2.
        mov ah, 02h
        mov al, BOOT_SECTORS
        mov bx, 7E00h
        mov cx, 2                       ; CH=cyl0, CL=sector2
        mov dx, bp                      ; DL=drive (DH cleared next)
        xor dh, dh                      ; head 0
        int 13h
        jc .error

        jmp post_mbr_continue

.error:
        mov ax, 0E00h | '!'
        xor bx, bx
        int 10h
.halt:
        hlt
        jmp .halt

        ;; boot_disk and directory_sector are written into kernel.bin's
        ;; embedded boot stash (BOOT_STASH_OFFSET) after the kernel.bin
        ;; load completes.  The kernel reads them through PDE[768]'s
        ;; direct map; no permanent low-physical reservation needed.

        times 506-($-$$) db 0
        ;; kernel_bytes (offset 506): total post-MBR bytes (boot's
        ;; post-MBR portion + kernel.bin).  Widened to a dword so the
        ;; kernel can exceed 64 KB without truncating the field.
        ;; Host-side add_file.py reads this to compute where the
        ;; directory begins on disk — the same arithmetic the boot path
        ;; runs at startup.
kernel_bytes dd (BOOT_SECTORS + KERNEL_SECTORS) * 512
        dw 0AA55h

;;; ----- Post-MBR boot region (0x7E00 onwards) -----

post_mbr_continue:
        ;; Compute directory_sector = ceil(kernel_bytes / 512) + 1, the
        ;; LBA of the first directory sector right after the kernel
        ;; image.  Cached in SI for the post-load stash write below;
        ;; can't be written into kernel.bin's embedded slot yet because
        ;; the INT 13h that loads kernel.bin would clobber it.
        ;; kernel_bytes is a dword (widened from word to support kernels
        ;; larger than 64 KB); the directory sector fits in 16 bits.
        mov eax, [kernel_bytes]
        add eax, 511
        shr eax, 9
        inc eax
        mov si, ax                      ; SI = directory_sector (fits in 16 bits)

        ;; Load kernel.bin from disk into physical KERNEL_LOAD_PHYS
        ;; (= 0x2000:0x0000 in real mode = 0x20000 linear).
        ;;
        ;; INT 13h-42h (extended LBA / EDD) is NOT supported by SeaBIOS
        ;; on emulated floppy drives — the call fails (CF=1) and halts
        ;; the boot.  Use INT 13h-02h (CHS read) instead, which works on
        ;; both floppy and HDD.
        ;;
        ;; Two constraints require chunking:
        ;;   1. ISA DMA 64 KB boundary: a read whose buffer spans a 64 KB
        ;;      linear boundary silently corrupts or returns an error.
        ;;      The buffer starts at phys 0x20000; each chunk must end
        ;;      before the next 64 KB boundary.
        ;;   2. Track boundary: INT 13h-02h cannot read past the end of a
        ;;      track in a single call; the sector count must not exceed
        ;;      the sectors remaining on the current track.
        ;;
        ;; Geometry is fetched at runtime via INT 13h-08h (Get Drive
        ;; Parameters) so this code works on any BIOS-supported drive
        ;; rather than only the two QEMU defaults.  Function 08h
        ;; returns CL[5:0] = max sector (1-based) → sectors_per_track,
        ;; DH = max head (0-based) → num_heads = DH + 1, and ES:DI =
        ;; floppy DPT pointer (clobbered, unused).
        ;;
        ;; Buffer management: the buffer segment (ES) is advanced by
        ;; N*32 paragraphs after each read of N sectors (N*512/16 = N*32).
        ;; This way ES:0 always points to the next write position without
        ;; needing a non-zero BX offset.
        ;;
        ;; DMA constraint: track [0x7B0A] = sectors already loaded in the
        ;; current 64 KB window.  limit_dma = 128 - [0x7B0A].  After
        ;; advancing ES by N*32, if the window is full (window_used = 128)
        ;; reset window_used to 0 (the segment already advanced past the
        ;; boundary).
        ;;
        ;; Scratch layout at 0x7B00:
        ;;   [0x7B00] byte  sectors_per_track
        ;;   [0x7B01] byte  num_heads
        ;;   [0x7B02] word  current CHS word (CH=cyl[7:0], CL=sec|cyl_hi)
        ;;   [0x7B04] byte  current head (DH for INT 13h-02h)
        ;;   [0x7B05] byte  sectors left on current track
        ;;   [0x7B06] word  total sectors remaining
        ;;   [0x7B08] word  buffer segment (advanced after each read)
        ;;   [0x7B0A] word  sectors loaded in current 64 KB DMA window

        push si                                ; save directory_sector
        push es                                ; INT 13h-08h clobbers ES:DI

        ;; Fetch drive geometry via INT 13h-08h (Get Drive Parameters).
        ;; CF=1 with AH=01h on drives that don't support function 08h
        ;; (rare on PCs post-1991); halt the boot on failure.
        mov dx, bp                             ; DL = drive number
        mov ah, 08h
        int 13h
        pop es
        jc .error_post
        and cl, 0x3F
        mov [0x7B00], cl                       ; sectors_per_track
        inc dh
        mov [0x7B01], dh                       ; num_heads


        ;; Convert starting LBA = 1 + BOOT_SECTORS to CHS.
        ;; track  = LBA / spt       cylinder = track / num_heads
        ;; sector = LBA mod spt + 1  head    = track mod num_heads
        mov ax, 1 + BOOT_SECTORS
        xor dx, dx
        xor ch, ch
        mov cl, [0x7B00]                      ; CX = spt (zero-extended)
        div cx                                 ; AX=track, DX=sector-1
        inc dl                                 ; DL = sector (1-based)
        push dx                               ; save sector
        xor dx, dx
        mov cl, [0x7B01]                      ; CX = num_heads
        div cx                                 ; AX=cylinder, DX=head
        pop cx                                ; CX = sector (low byte)
        ;; Encode CHS word: CH=cyl[7:0], CL=sector[5:0]|cyl[9:8]<<6
        push dx                               ; save head
        mov bx, ax
        shr bx, 2
        and bx, 0xC0                          ; BL = cyl[9:8] << 6
        or cl, bl
        mov ch, al                            ; CH = cyl[7:0]
        mov [0x7B02], cx
        pop dx
        mov [0x7B04], dl                      ; store head
        ;; sectors_left_on_track = spt - sector + 1
        mov al, cl
        and al, 0x3F                          ; AL = sector
        mov bl, [0x7B00]
        sub bl, al
        inc bl
        mov [0x7B05], bl
        mov word [0x7B06], KERNEL_SECTORS
        mov word [0x7B08], 0x2000             ; buffer segment
        mov word [0x7B0A], 0                  ; DMA window sectors used

.kernel_read_loop:
        ;; count = min(track_left, dma_left, remaining, 64)
        xor bh, bh
        mov bl, [0x7B05]                      ; BX = track_left
        mov ax, 128
        sub ax, [0x7B0A]                      ; AX = dma_left
        cmp ax, bx
        jbe .krl_have_min
        mov ax, bx
.krl_have_min:
        cmp ax, [0x7B06]
        jbe .krl_cap_remain
        mov ax, [0x7B06]
.krl_cap_remain:
        cmp ax, 64
        jbe .krl_do_read
        mov ax, 64
.krl_do_read:
        ;; Issue INT 13h-02h.
        mov cx, [0x7B02]
        mov bx, [0x7B08]
        mov es, bx
        xor bx, bx
        mov ah, 02h
        mov dx, bp                            ; DL = drive
        mov dh, [0x7B04]                      ; DH = head
        int 13h
        jc .error_post

        ;; AL = sectors actually read (set by BIOS).
        xor ah, ah
        sub [0x7B06], ax                      ; remaining -= read
        sub [0x7B05], al                      ; track_left -= read
        add [0x7B0A], ax                      ; window_used += read
        ;; Advance buffer segment: N sectors = N*32 paragraphs.
        mov bx, ax
        shl bx, 5                             ; BX = N * 32
        add [0x7B08], bx

        ;; If DMA window full, reset counter (segment already advanced).
        cmp word [0x7B0A], 128
        jb .krl_window_ok
        mov word [0x7B0A], 0
.krl_window_ok:

        ;; Advance sector in CHS word: CL[5:0] += sectors_read.
        ;; This is needed when the track is not fully exhausted so
        ;; the next read starts at the correct sector.
        mov cx, [0x7B02]                      ; CX = current CHS
        mov bx, ax                            ; BX = sectors read (AX, AH=0)
        add cl, bl                            ; CL += sectors_read (within track)

        ;; If track exhausted, advance to next track.
        cmp byte [0x7B05], 0
        jne .krl_store_chs
        mov cl, 1                             ; sector = 1 for next track
        mov al, [0x7B04]
        inc al
        cmp al, [0x7B01]
        jb .krl_new_head                      ; still on same cylinder
        ;; Wrap head, increment cylinder.
        xor al, al
        mov bl, ch
        mov bh, cl
        shr bh, 6                             ; BH = cyl[9:8]
        inc bx                                ; cylinder++
        mov ch, bl
        and cl, 0x3F
        mov bl, bh
        shl bl, 6
        or cl, bl
.krl_new_head:
        mov [0x7B04], al
        mov al, [0x7B00]
        mov [0x7B05], al                      ; track_left = spt
.krl_store_chs:
        mov [0x7B02], cx                      ; update CHS word
.krl_same_track:
        cmp word [0x7B06], 0
        jne .kernel_read_loop

.kernel_read_done:
        xor ax, ax
        mov es, ax
        pop si                                 ; restore directory_sector

        ;; Stash boot_disk and directory_sector into kernel.bin's
        ;; embedded slot (BOOT_STASH_OFFSET).  Reload ES = 0x2000.
        ;; ES:BOOT_STASH_OFFSET = phys 0x20000 + offset = the
        ;; boot_disk byte (followed by directory_sector dw).
        mov ax, 2000h
        mov es, ax
        mov ax, bp                               ; AL = drive number
        mov [es:BOOT_STASH_OFFSET], al           ; boot_disk (1 byte)
        mov [es:BOOT_STASH_OFFSET + 1], si       ; directory_sector (2 bytes)

        ;; Reset ES so the rest of the real-mode code addresses
        ;; segment 0 normally.
        xor ax, ax
        mov es, ax

        ;; Copy the BIOS ROM 8x16 font into char-gen plane 2 offset
        ;; 0x4000 (slot the mode-03h table's SR03=05h points at)
        ;; before BIOS goes away with the protected mode flip.
        ;; Without this, switching back to text mode after running a
        ;; graphics program (e.g. draw) leaves the character generator
        ;; pointed at empty VRAM.
        call vga_font_load

        ;; Walk the BIOS memory map via INT 15h AX=E820.  Stash 24-byte
        ;; entries at physical 0x500, terminated by a 24-byte zero
        ;; entry.  The bitmap frame allocator (post-paging) consumes
        ;; this to know which physical regions are usable RAM.
        mov di, 0x500
        xor ebx, ebx                    ; continuation token, 0 = start
        mov edx, 0x534D4150             ; 'SMAP' signature
.e820_loop:
        mov eax, 0x0000E820
        mov ecx, 24
        mov dword [di + 20], 1          ; default ACPI attrs = "valid + ignore-on-read"
        int 15h
        jc .e820_done                   ; CF set = no support or end
        cmp eax, 0x534D4150
        jne .e820_done
        test ecx, ecx
        jz .e820_skip                   ; zero-length entry, skip but keep walking
        cmp ecx, 20
        jb .e820_skip
        add di, 24
.e820_skip:
        test ebx, ebx
        jnz .e820_loop
.e820_done:
        ;; Write the 24-byte zero terminator at DI.
        push di
        xor eax, eax
        mov cx, 24 / 2
        rep stosw
        pop di

        ;; Remap 8259A master/slave vectors to 0x20..0x27 / 0x28..0x2F.
        ;; Required before the protected mode flip — CPU exceptions 0..31
        ;; would otherwise alias onto IRQ vectors.  Leaves all IRQ lines
        ;; masked; the kernel unmasks the IRQs it actually owns.
        mov al, ICW1_INIT
        out PIC1_CMD_PORT, al
        out PIC2_CMD_PORT, al

        mov al, PIC1_VECTOR
        out PIC1_DATA_PORT, al
        mov al, PIC2_VECTOR
        out PIC2_DATA_PORT, al

        mov al, PIC1_CASCADE
        out PIC1_DATA_PORT, al
        mov al, PIC2_CASCADE_ID
        out PIC2_DATA_PORT, al

        mov al, ICW4_8086
        out PIC1_DATA_PORT, al
        out PIC2_DATA_PORT, al

        mov al, PIC_MASK_ALL
        out PIC1_DATA_PORT, al
        out PIC2_DATA_PORT, al

        ;; Fast-A20 via port 0x92, bit 1.  Bit 0 triggers a warm
        ;; reset, so mask it off before writing.
        in al, 0x92
        test al, 0x02
        jnz .a20_ready
        or al, 0x02
        and al, 0xFE
        out 0x92, al
.a20_ready:

        lgdt [pmode_gdtr]

        mov eax, cr0
        or eax, 1
        mov cr0, eax

        jmp dword BOOT_CODE_SELECTOR:early_pe_entry

.error_post:
        ;; INT 13h read of kernel.bin failed.  We're still in real mode
        ;; here so BIOS print is available.
        mov ax, 0E00h | 'K'
        xor bx, bx
        int 10h
.halt_post:
        hlt
        jmp .halt_post

%include "vga_font.asm"

[bits 32]
early_pe_entry:
        ;; Reload segment registers with the kernel data selector.
        ;; The far-jump from the PE flip already loaded CS.
        mov ax, BOOT_DATA_SELECTOR
        mov ds, ax
        mov es, ax
        mov ss, ax
        mov fs, ax
        mov gs, ax
        mov esp, 0x9FFF0                ; pre-paging stack (still low memory)

        ;; Step 1: Build the first kernel PT at FIRST_KERNEL_PT_PHYS.
        ;; PTE[j] = (j * 0x1000) | P | RW | G  (U/S=0, kernel-only).
        ;; This single PT covers physical 0..4 MB and is hooked into
        ;; the boot PD twice — at PDE[0] (identity) and
        ;; PDE[FIRST_KERNEL_PDE] (kernel direct map at virt
        ;; KERNEL_VIRT_BASE).
        mov edi, FIRST_KERNEL_PT_PHYS
        xor ecx, ecx
.fill_pt:
        mov eax, ecx
        shl eax, 12
        or eax, 0x103                   ; P | RW | G
        mov [edi + ecx*4], eax
        inc ecx
        cmp ecx, 1024
        jb .fill_pt

        ;; Step 2: Zero the boot PD at BOOT_PD_PHYS, then install the
        ;; first kernel PT at PDE[0] (identity for first 4 MB) and
        ;; PDE[FIRST_KERNEL_PDE] (kernel direct map at virt
        ;; KERNEL_VIRT_BASE..KERNEL_VIRT_BASE+0x3FFFFF).
        mov edi, BOOT_PD_PHYS
        xor eax, eax
        mov ecx, 1024
        rep stosd

        mov dword [BOOT_PD_PHYS + 0*4], FIRST_KERNEL_PT_PHYS | 0x003
        mov dword [BOOT_PD_PHYS + FIRST_KERNEL_PDE*4], FIRST_KERNEL_PT_PHYS | 0x003

        ;; Step 3: Set CR3 = boot PD, enable PG | WP in CR0.
        ;; CR0.WP makes ring-0 writes honor R/W bits in PTEs (so a
        ;; kernel write through a read-only user page #PFs instead of
        ;; silently succeeding).
        mov eax, BOOT_PD_PHYS
        mov cr3, eax
        mov eax, cr0
        or eax, 0x80010000              ; CR0.PG | CR0.WP
        mov cr0, eax

        ;; Step 4: Far-jump to the high-half kernel entry.  EIP becomes
        ;; the kernel-virt address of high_entry (= the first byte of
        ;; kernel.bin per its `org 0xC0020000`).  The identity map at
        ;; PDE[0] keeps low-physical addresses (boot.asm's GDT etc.)
        ;; reachable until the kernel re-lgdts and tears identity down.
        jmp dword BOOT_CODE_SELECTOR:HIGH_ENTRY_VIRT

;;; ----- GDT shared by boot.asm and the early-PE bootstrap -----
;;;
;;; The kernel installs its own copy at virt 0xC01xxxxx via lgdt
;;; once paging is on; from that point this table can disappear with
;;; the rest of low physical memory when identity is dropped.
        align 8
pmode_gdt_start:
        dq 0                            ; 0x00 null

        ;; 0x08 code: base=0, limit=0xFFFFF (×4 KB = 4 GB).
        ;; Access 10011010b  = P=1 DPL=00 S=1 type=1010 (exec/read)
        ;; Flags  11001111b  = G=1 D=1 L=0 AVL=0, limit[19:16]=0xF
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 10011010b
        db 11001111b
        db 0x00

        ;; 0x10 data: same geometry, type=0010 (R/W).
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 10010010b
        db 11001111b
        db 0x00

        ;; 0x18 user code: same as kernel code, DPL=3.
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 11111010b
        db 11001111b
        db 0x00

        ;; 0x20 user data: same as kernel data, DPL=3.
        dw 0xFFFF
        dw 0x0000
        db 0x00
        db 11110010b
        db 11001111b
        db 0x00

        ;; 0x28 TSS placeholder.  The kernel rebuilds and re-lgdts its
        ;; own GDT once paging is on, so this entry never gets used —
        ;; it's here only so kernel-mode segment selectors line up.
        dw 103
        dw 0x0000
        db 0x00
        db 10001001b
        db 00000000b
        db 0x00
pmode_gdt_end:

pmode_gdtr:
        dw pmode_gdt_end - pmode_gdt_start - 1
        dd pmode_gdt_start

        ;; Round the post-MBR region up to the next 512-byte boundary
        ;; so the MBR's INT 13h read pulls a whole number of sectors.
        ;; BOOT_SECTORS below derives the count from the resulting
        ;; size, so the post-MBR code can grow into a new sector
        ;; without any manual bump.
        times (-($ - post_mbr_continue)) & 511 db 0
boot_end:

        ;; Forward-resolved post-MBR sector count.  NASM resolves
        ;; `equ` references multi-pass; every use of BOOT_SECTORS
        ;; above (line 95 imm8, line 124 `dw` expression, line 152
        ;; imm8) is a fixed-size operand, so the value can settle
        ;; without changing any instruction's encoded width.
        BOOT_SECTORS    equ (boot_end - post_mbr_continue) / 512

        ;; Build-time guard: `kernel_bytes` (MBR offset 506) is a 32-bit
        ;; dword; total post-MBR bytes must fit in 0xFFFFFFFF and the
        ;; resulting directory_sector must fit in a 16-bit SI register.
        ;; (KERNEL_SECTORS * 512 < 512 * 65535 ~= 32 MB easily fits.)
        ;; Placed after BOOT_SECTORS' equ so the times argument is a
        ;; critical expression by NASM's pass rules.
        times -(((BOOT_SECTORS + KERNEL_SECTORS) * 512) > 0xFFFFFFFF) db 0
