// fdc.c — native floppy disk controller driver using DMA + IRQ 6.
//
// Mirrors SeaBIOS's flow: sector data moves through 8237 DMA channel 2
// and command completion is signalled by IRQ 6.  Target: primary
// 82077-style controller at 0x3F0..0x3F7, drive 0 (A:),
// 1.44 MB geometry (80 cyl × 2 heads × 18 sectors).
//
// Surface (parallel to ata.c):
//   fdc_init          install IRQ 6 handler, reset, SPECIFY, recalibrate
//   fdc_read_sector   AX = 0-based LBA; fills sector_buffer; CF clear
//   fdc_write_sector  AX = 0-based LBA; writes sector_buffer; CF clear
//
// Constants inlined as bare integers to avoid clashing with the
// shared %include namespace.  Key values:
//   FDC_DOR/MSR/DATA/CCR     = 0x3F2 / 0x3F4 / 0x3F5 / 0x3F7
//   DOR_RESET_NOT/DMA_IRQ/MOTOR_0 = 0x04 / 0x08 / 0x10
//   MSR_RQM/DIO              = 0x80 / 0x40
//   CMD_SPECIFY/RECAL/SENSE/SEEK/READ/WRITE = 0x03/0x07/0x08/0x0F/0xE6/0xC5
//   FDC_SECTORS_PER_TRACK = 18, FDC_HEADS = 2, FDC_GAP3 = 0x1B
//   FDC_SECTOR_SIZE_CODE = 2 (= 2^2 * 128 = 512)
//   DMA_CH2_ADDR/COUNT/PAGE = 0x04 / 0x05 / 0x81
//   DMA_MASK/MODE/CLEAR_FF  = 0x0A / 0x0B / 0x0C
//   DMA_MODE_READ/WRITE     = 0x46 / 0x4A
//   DMA_MASK_CH2/UNMASK_CH2 = 0x06 / 0x02
//   FDC_IRQ6_VECTOR         = 0x26  (post pic_remap)
//   PIC1_CMD_PORT / PIC1_DATA_PORT = 0x20 / 0x21
//   PIC_EOI = 0x20  (also in src/include/constants.asm)

uint8_t fdc_irq_flag;
uint8_t fdc_motor_ready;

// FS scratch frame pointer — defined in vfs.c, populated by
// `vfs_init` before the first disk read.  fdc_dma_setup feeds the
// low 24 bits of this pointer to the 8237 as the transfer's
// physical address; the frame_alloc-managed pool sits inside the
// kernel direct map (phys 0..1 GB at LAST_KERNEL_PDE = 1024), so
// kernel-virt minus 0xC0000000 equals phys.  The 8237 only takes 24
// bits, so a frame above the 16 MB ISA-DMA ceiling would still be a
// problem here; vfs_init's first-fit allocation lands in low memory
// in practice.
extern uint8_t *sector_buffer;

// Forward declarations for callees that come later alphabetically and
// are invoked from earlier-alphabetical C functions.  asm-body
// callees (resolved at NASM time inside other asm() blocks) don't
// need a C-level forward decl, so only the cross-C-callsite chains
// land here.
void fdc_install_irq();
int fdc_recv() __attribute__((preserve_register("edx")));
void fdc_seek(int cx_arg __attribute__((in_register("cx"))),
              int dx_arg __attribute__((in_register("dx"))));
void fdc_send(uint8_t byte __attribute__((in_register("ax"))));
void fdc_sense_interrupt();
void fdc_wait_irq();

// Program 8237 DMA channel 2 for a 512-byte transfer at sector_buffer.
// AL = mode byte (DMA_MODE_READ=0x46 or DMA_MODE_WRITE=0x4A).  The
// 8237 takes a 24-bit physical address; we pass the low 24 bits of
// sector_buffer's kernel-virt, which equals the actual frame phys
// because the bitmap allocator hands out frames inside the kernel
// direct map and 0xC0000000's bit 23..0 are zero.
void fdc_dma_setup(uint8_t mode __attribute__((in_register("ax"))))
    __attribute__((preserve_register("eax")))
{
    int phys;
    phys = sector_buffer;
    kernel_outb(0x0A, 0x06);                       // mask channel 2
    kernel_outb(0x0C, 0);                          // clear flip-flop
    kernel_outb(0x04, phys & 0xFF);                // addr low
    kernel_outb(0x04, (phys >> 8) & 0xFF);         // addr high
    kernel_outb(0x0C, 0);                          // clear flip-flop again
    kernel_outb(0x05, (512 - 1) & 0xFF);           // count low
    kernel_outb(0x05, ((512 - 1) >> 8) & 0xFF);    // count high
    kernel_outb(0x0B, mode);                       // DMA mode
    kernel_outb(0x81, (phys >> 16) & 0xFF);        // page
    kernel_outb(0x0A, 0x02);                       // unmask channel 2
}

// Drain the 7 result bytes (ST0, ST1, ST2, C, H, R, N) — ignored.
void fdc_drain_result() {
    uint8_t index;
    index = 0;
    while (index < 7) {
        fdc_recv();
        index = index + 1;
    }
}

// One-time init.  Install IRQ 6 handler + unmask, reset controller,
// SPECIFY in DMA mode.  Motor stays off until first read or write.
void fdc_init() {
    uint8_t mask;

    fdc_install_irq();
    mask = kernel_inb(0x21);                  // PIC1_DATA_PORT
    kernel_outb(0x21, mask & 0xBF);           // clear bit 6 (unmask IRQ 6)

    fdc_irq_flag = 0;

    // Reset: clear DOR, then raise RESET_NOT with DMA+IRQ + drive 0.
    kernel_outb(0x3F2, 0);                    // FDC_DOR
    kernel_outb(0x3F2, 0x0C);                 // RESET_NOT | DMA_IRQ
    fdc_wait_irq();                           // controller signals ready

    // Drain 4 polling interrupts (one per drive slot on 82077AA).
    fdc_sense_interrupt();
    fdc_sense_interrupt();
    fdc_sense_interrupt();
    fdc_sense_interrupt();

    // Data rate 500 Kbps for 1.44 MB.
    kernel_outb(0x3F7, 0);                    // FDC_CCR

    // SPECIFY: SRT/HUT don't matter on QEMU; HLT=1, ND=0 (DMA).
    fdc_send(0x03);                           // CMD_SPECIFY
    fdc_send(0xDF);
    fdc_send(0x02);
}

// Install fdc_irq6_handler at FDC_IRQ6_VECTOR via idt_set_gate32.
// idt_set_gate32 takes EAX = handler address, BL = vector — cc.py
// can't address-of a label, so this is an asm() block.
asm("fdc_install_irq:\n"
    "    push eax\n"
    "    push ebx\n"
    "    mov eax, fdc_irq6_handler\n"
    "    mov bl, 0x26\n"
    "    call idt_set_gate32\n"
    "    pop ebx\n"
    "    pop eax\n"
    "    ret");

// IRQ 6 stub — flag that the controller has signalled completion,
// EOI to the master PIC, iretd.  Installed at FDC_IRQ6_VECTOR
// (0x26) by fdc_install_irq.  Same shape as ps2_irq1_handler.
asm("fdc_irq6_handler:\n"
    "    push eax\n"
    "    mov byte [_g_fdc_irq_flag], 1\n"
    "    mov al, 0x20\n"           // PIC_EOI
    "    out 0x20, al\n"           // PIC1_CMD_PORT
    "    pop eax\n"
    "    iretd");

// Issue a READ or WRITE command with the 9-byte parameter sequence.
// AL = command, CH = cyl, CL = sec (1-based), DH = head.
void fdc_issue_read_write(uint8_t command __attribute__((in_register("ax"))),
                          int cx_arg __attribute__((in_register("cx"))),
                          int dx_arg __attribute__((in_register("dx"))));

asm("fdc_issue_read_write:\n"
    "    push eax\n"
    "    push ebx\n"
    "    push ecx\n"
    "    push edx\n"
    "    mov bh, al\n"             // stash command
    "    call fdc_send\n"          // command
    "    mov al, dh\n"
    "    shl al, 2\n"
    "    call fdc_send\n"          // HDS = (head<<2) | drive(0)
    "    mov al, ch\n"
    "    call fdc_send\n"          // C
    "    mov al, dh\n"
    "    call fdc_send\n"          // H
    "    mov al, cl\n"
    "    call fdc_send\n"          // R (1-based sector)
    "    mov al, 2\n"              // FDC_SECTOR_SIZE_CODE
    "    call fdc_send\n"          // N
    "    mov al, cl\n"
    "    call fdc_send\n"          // EOT = R → 1-sector transfer
    "    mov al, 0x1B\n"           // FDC_GAP3
    "    call fdc_send\n"          // GPL
    "    mov al, 0xFF\n"
    "    call fdc_send\n"          // DTL (ignored when N>0)
    "    pop edx\n"
    "    pop ecx\n"
    "    pop ebx\n"
    "    pop eax\n"
    "    ret");

// LBA → CHS: CH = cyl, CL = sec (1-based), DH = head.
// AX = LBA in.  CHS multi-byte return goes through `out_register`
// parameters — caller passes &cx and &dx and gets the packed
// CH:CL / DH:DL pair captured.
void fdc_lba_to_chs_internal(int lba __attribute__((in_register("ax"))),
                             int *cx_out __attribute__((out_register("cx"))),
                             int *dx_out __attribute__((out_register("dx"))));

asm("fdc_lba_to_chs_internal:\n"
    "    push eax\n"
    "    push ebx\n"
    "    xor dx, dx\n"
    "    mov bx, 18\n"             // FDC_SECTORS_PER_TRACK
    "    div bx\n"
    "    mov cl, dl\n"
    "    inc cl\n"                 // 1-based sector
    "    xor dx, dx\n"
    "    mov bx, 2\n"              // FDC_HEADS
    "    div bx\n"
    "    mov ch, al\n"
    "    mov dh, dl\n"
    "    pop ebx\n"
    "    pop eax\n"
    "    ret");

// Turn motor 0 on, wait 500 ms for spin-up, recalibrate drive 0.
// Called lazily on the first read or write.  Motor stays on for the
// lifetime of the session.
void fdc_motor_start();

asm("fdc_motor_start:\n"
    "    push eax\n"
    "    push ecx\n"
    "    push edx\n"
    "    mov dx, 0x3F2\n"          // FDC_DOR
    "    mov al, 0x1C\n"           // MOTOR_0 | RESET_NOT | DMA_IRQ
    "    out dx, al\n"
    "    mov cx, 500\n"
    "    call rtc_sleep_ms\n"
    "    mov byte [_g_fdc_irq_flag], 0\n"
    "    mov al, 0x07\n"           // CMD_RECALIBRATE
    "    call fdc_send\n"
    "    xor al, al\n"             // drive 0
    "    call fdc_send\n"
    "    call fdc_wait_irq\n"
    "    call fdc_sense_interrupt\n"
    "    mov byte [_g_fdc_motor_ready], 1\n"
    "    pop edx\n"
    "    pop ecx\n"
    "    pop eax\n"
    "    ret");

// AX = LBA → sector_buffer filled via DMA.  CF=0 (always — there's
// no error path; failures hang in fdc_wait_irq, matching the asm).
// cc.py's carry_return convention: return 1 → CF clear (success).
int fdc_read_sector(int lba __attribute__((in_register("ax"))))
    __attribute__((carry_return))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
{
    int cx;
    int dx;

    if (fdc_motor_ready == 0) {
        fdc_motor_start();
    }
    fdc_lba_to_chs_internal(lba, &cx, &dx);
    fdc_seek(cx, dx);
    fdc_dma_setup(0x46);                      // DMA_MODE_READ
    fdc_irq_flag = 0;
    fdc_issue_read_write(0xE6, cx, dx);       // CMD_READ
    fdc_wait_irq();
    fdc_drain_result();
    return 1;
}

// Wait for RQM=1, DIO=1 (host can read), then read one byte from
// FDC data.  Returns AL = byte; clobbers AX, DX (matches asm).
asm("fdc_recv:\n"
    "    push edx\n"
    ".fdc_recv_wait:\n"
    "    mov dx, 0x3F4\n"
    "    in al, dx\n"
    "    and al, 0xC0\n"
    "    cmp al, 0xC0\n"            // RQM=1, DIO=1 (host reads)
    "    jne .fdc_recv_wait\n"
    "    mov dx, 0x3F5\n"
    "    in al, dx\n"
    "    pop edx\n"
    "    ret");

// SEEK to a track on a head.  CH = cyl, DH = head.
asm("fdc_seek:\n"
    "    push eax\n"
    "    mov byte [_g_fdc_irq_flag], 0\n"
    "    mov al, 0x0F\n"           // CMD_SEEK
    "    call fdc_send\n"
    "    mov al, dh\n"
    "    shl al, 2\n"
    "    call fdc_send\n"          // HDS
    "    mov al, ch\n"
    "    call fdc_send\n"          // cylinder
    "    call fdc_wait_irq\n"
    "    call fdc_sense_interrupt\n"
    "    pop eax\n"
    "    ret");

// Wait for the FDC's MSR to show RQM=1, DIO=0 (host can write), then
// send the byte to the data register.  Preserves AX, DX (used for
// the polling cursor) — same shape as the asm version.
asm("fdc_send:\n"
    "    push eax\n"
    "    push edx\n"
    "    mov ah, al\n"
    ".fdc_send_wait:\n"
    "    mov dx, 0x3F4\n"           // FDC_MSR
    "    in al, dx\n"
    "    and al, 0xC0\n"            // MSR_RQM | MSR_DIO
    "    cmp al, 0x80\n"            // RQM=1, DIO=0 (host writes)
    "    jne .fdc_send_wait\n"
    "    mov dx, 0x3F5\n"           // FDC_DATA
    "    mov al, ah\n"
    "    out dx, al\n"
    "    pop edx\n"
    "    pop eax\n"
    "    ret");

// Issue a sense-interrupt and discard ST0/PCN.  Used during reset
// and after every SEEK / RECAL.
void fdc_sense_interrupt() {
    fdc_send(0x08);  // CMD_SENSE_INT
    fdc_recv();      // ST0
    fdc_recv();      // PCN
}

// Block until IRQ 6 sets fdc_irq_flag.  pushf/sti envelope keeps
// the caller's IF intact while ensuring IRQ 6 fires inside an
// IF=0 syscall context.
asm("fdc_wait_irq:\n"
    "    pushf\n"
    "    sti\n"
    ".fdc_wait_irq_loop:\n"
    "    cmp byte [_g_fdc_irq_flag], 0\n"
    "    je .fdc_wait_irq_loop\n"
    "    mov byte [_g_fdc_irq_flag], 0\n"
    "    popf\n"
    "    ret");

// AX = LBA, sector_buffer → disk.  Same return shape as read.
int fdc_write_sector(int lba __attribute__((in_register("ax"))))
    __attribute__((carry_return))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ebx")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
{
    int cx;
    int dx;

    if (fdc_motor_ready == 0) {
        fdc_motor_start();
    }
    fdc_lba_to_chs_internal(lba, &cx, &dx);
    fdc_seek(cx, dx);
    fdc_dma_setup(0x4A);                      // DMA_MODE_WRITE
    fdc_irq_flag = 0;
    fdc_issue_read_write(0xC5, cx, dx);       // CMD_WRITE
    fdc_wait_irq();
    fdc_drain_result();
    return 1;
}
