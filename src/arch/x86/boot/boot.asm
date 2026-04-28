;;; ------------------------------------------------------------------------
;;; boot.asm — pre-paging boot binary.
;;;
;;; Layout (org 0x7C00):
;;;   [0x7C00..0x7DFF]  MBR — disk reset + INT 13h read of post-MBR
;;;                      boot bytes; jumps into the post-MBR region
;;;                      once those sectors are resident.
;;;   [0x7E00..]        Post-MBR real-mode bootstrap: second INT 13h
;;;                      read pulls kernel.bin into physical 0x10000,
;;;                      then VGA font copy / E820 probe / PIC remap /
;;;                      A20 / GDT / CR0.PE flip / far-jump into the
;;;                      32-bit `early_pe_entry`.  Padded to a fixed
;;;                      BOOT_SECTORS so the build script only has to
;;;                      measure kernel.bin's sector count.
;;;
;;; After the PE flip, `early_pe_entry` runs in flat 32-bit at low
;;; physical, copies kernel.bin from 0x10000 to 0x100000, builds the
;;; boot PD + first kernel PT, enables paging, and far-jumps to
;;; `high_entry` at the kernel-virt entry point (0xC0100000 — the very
;;; first byte of kernel.bin per its `org` directive).
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

        ;; Physical addresses for early-PE page-table setup.
        BOOT_PD_PHYS            equ 0x1000
        FIRST_KERNEL_PT_PHYS    equ 0x2000
        KERNEL_LOAD_PHYS        equ 0x10000     ; INT 13h read destination
        KERNEL_FINAL_PHYS       equ 0x100000    ; final post-relocation phys
        HIGH_ENTRY_VIRT         equ 0xC0100000  ; kernel.bin org / first byte

start:
        xor ax, ax
        mov ds, ax
        mov es, ax
        mov [BOOT_DISK_PHYS], dl

        ;; Dedicated stack at SS=0x9000, SP=0xFFF0 (linear 0x90000-0x9FFF0)
        ;; owns its entire segment so it can never collide with the
        ;; kernel image at 0x7C00 / 0x10000 / 0x100000.
        cli
        mov ax, 9000h
        mov ss, ax
        mov sp, 0FFF0h
        sti

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
        mov dh, 0
        mov dl, [BOOT_DISK_PHYS]
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

        ;; boot_disk and directory_sector live at fixed low-physical
        ;; addresses (BOOT_DISK_PHYS / DIRECTORY_SECTOR_PHYS) so the
        ;; high-half kernel can read them through the direct map after
        ;; paging is on.  No storage in the boot binary itself.

        times 508-($-$$) db 0
        ;; kernel_bytes (offset 508): total post-MBR bytes (boot's
        ;; post-MBR portion + kernel.bin).  Host-side add_file.py
        ;; reads this to compute where the directory begins on disk —
        ;; the same arithmetic the boot path runs at startup.
kernel_bytes dw (BOOT_SECTORS + KERNEL_SECTORS) * 512
        dw 0AA55h

;;; ----- Post-MBR boot region (0x7E00 onwards) -----

post_mbr_continue:
        ;; Compute and stash directory_sector for the filesystem layer.
        ;; directory_sector = ceil(kernel_bytes / 512) + 1 = the LBA
        ;; of the first directory sector right after the kernel image.
        ;; Stored at fixed low-phys DIRECTORY_SECTOR_PHYS so the high-
        ;; half kernel can read it through the direct map.
        mov ax, [kernel_bytes]
        add ax, 511
        shr ax, 9
        inc ax
        mov [DIRECTORY_SECTOR_PHYS], ax

        ;; Load kernel.bin from disk into physical KERNEL_LOAD_PHYS
        ;; (= 0x1000:0x0000 in real mode = 0x10000 linear).  The
        ;; sector count is passed by the build script; the start CHS
        ;; sector is the first sector after boot.bin (1 MBR + BOOT_SECTORS
        ;; post-MBR ⇒ sector BOOT_SECTORS + 2 in 1-based CHS).
        mov ax, 1000h
        mov es, ax
        xor bx, bx
        mov ah, 02h
        mov al, KERNEL_SECTORS
        mov ch, 0
        mov dh, 0
        mov cl, BOOT_SECTORS + 2
        mov dl, [BOOT_DISK_PHYS]
        int 13h
        jc .error_post

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

        ;; Step 1: Copy kernel.bin from KERNEL_LOAD_PHYS to
        ;; KERNEL_FINAL_PHYS.  KERNEL_SECTORS * 512 bytes / 4 dwords.
        mov esi, KERNEL_LOAD_PHYS
        mov edi, KERNEL_FINAL_PHYS
        mov ecx, KERNEL_SECTORS * 512 / 4
        cld
        rep movsd

        ;; Step 2: Build the first kernel PT at FIRST_KERNEL_PT_PHYS.
        ;; PTE[j] = (j * 0x1000) | P | RW | G  (U/S=0, kernel-only).
        ;; This single PT covers physical 0..4 MB and is hooked into
        ;; the boot PD twice — at PDE[0] (identity) and PDE[768]
        ;; (kernel direct map at virt 0xC0000000).
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

        ;; Step 3: Zero the boot PD at BOOT_PD_PHYS, then install the
        ;; first kernel PT at PDE[0] (identity for first 4 MB) and
        ;; PDE[768] (kernel direct map at virt 0xC0000000..0xC03FFFFF).
        mov edi, BOOT_PD_PHYS
        xor eax, eax
        mov ecx, 1024
        rep stosd

        mov dword [BOOT_PD_PHYS + 0*4], FIRST_KERNEL_PT_PHYS | 0x003
        mov dword [BOOT_PD_PHYS + 768*4], FIRST_KERNEL_PT_PHYS | 0x003

        ;; Step 4: Set CR3 = boot PD, enable PG | WP in CR0.
        ;; CR0.WP makes ring-0 writes honor R/W bits in PTEs (so a
        ;; kernel write through a read-only user page #PFs instead of
        ;; silently succeeding).
        mov eax, BOOT_PD_PHYS
        mov cr3, eax
        mov eax, cr0
        or eax, 0x80010000              ; CR0.PG | CR0.WP
        mov cr0, eax

        ;; Step 5: Far-jump to the high-half kernel entry.  EIP becomes
        ;; the kernel-virt address of high_entry (= the first byte of
        ;; kernel.bin per its `org 0xC0100000`).  The identity map at
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

        ;; Build-time guard: `kernel_bytes` (above, MBR offset 508) is
        ;; a 16-bit dw, so total post-MBR bytes must fit in 0xFFFF.
        ;; If the kernel grows past that, the dw silently truncates
        ;; and add_file.py's directory-sector arithmetic wraps.  The
        ;; expression below evaluates to 1 on overflow, so `times -1
        ;; db 0` fails with "TIMES value is negative" and points the
        ;; next reader here.  When it trips: widen `kernel_bytes` to
        ;; a dword (and the matching reader in add_file.py +
        ;; post_mbr_continue's directory_sector compute).  Placed
        ;; after BOOT_SECTORS' equ so the times argument is a critical
        ;; expression by NASM's pass rules.
        times -(((BOOT_SECTORS + KERNEL_SECTORS) * 512) > 0xFFFF) db 0
