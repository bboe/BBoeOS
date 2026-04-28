// fdc.c — native floppy disk controller driver using DMA + IRQ 6.
//
// Mirrors SeaBIOS's flow (src/hw/floppy.c): sector data moves through
// 8237 DMA channel 2 and command completion is signalled by IRQ 6.
// That's the path QEMU's FDC emulation is battle-tested against; an
// earlier polled / PIO attempt using the FIFO hung after the first
// successful read because the state machine drifted out of sync.
//
// Target: primary 82077-style controller at 0x3F0..0x3F7, drive 0 (A:),
// 1.44 MB geometry (80 cyl × 2 heads × 18 sectors).
//
// Surface (parallel to ata.c):
//     fdc_init          install IRQ 6 handler, reset, SPECIFY, motor on,
//                       recalibrate drive 0.  Called once.
//     fdc_read_sector   AX = 0-based LBA; fills SECTOR_BUFFER; CF err.
//     fdc_write_sector  AX = 0-based LBA; writes SECTOR_BUFFER; CF err.
//
// fdc_irq6_handler / fdc_install_irq / fdc_wait_irq stay in a file-scope
// asm() block at the bottom: the ISR has to ``iret`` not ``ret``,
// fdc_install_irq writes the IVT (touches CS / ES) and toggles the PIC
// mask bit through inline IN / OUT, and fdc_wait_irq has to pushf/sti
// around the busy-wait so it doesn't permanently re-enable interrupts
// for the caller.  None of these are expressible cleanly in cc.py's
// C subset.

// Floppy controller ports / commands.  ``FDC_`` prefix — naked names like
// ``DOR`` would collide with NASM ``equ``s elsewhere in the kernel
// (cc.py emits ``#define`` as ``%define``, which mass-substitutes).
#define FDC_DOR  0x3F2
#define FDC_MSR  0x3F4
#define FDC_DATA 0x3F5
#define FDC_CCR  0x3F7

#define FDC_DOR_RESET_NOT 0x04
#define FDC_DOR_DMA_IRQ   0x08
#define FDC_DOR_MOTOR_0   0x10

#define FDC_MSR_RQM 0x80
#define FDC_MSR_DIO 0x40

#define FDC_CMD_SPECIFY     0x03
#define FDC_CMD_RECALIBRATE 0x07
#define FDC_CMD_SENSE_INT   0x08
#define FDC_CMD_SEEK        0x0F
#define FDC_CMD_READ        0xE6  // MT=1 MF=1 SK=1
#define FDC_CMD_WRITE       0xC5  // MT=1 MF=1

#define FDC_SECTORS_PER_TRACK 18
#define FDC_SECTOR_SIZE_CODE  2     // 2^N * 128 = 512
#define FDC_GAP3              0x1B

// 8237 DMA channel 2.
#define FDC_DMA_CH2_ADDR  0x04
#define FDC_DMA_CH2_COUNT 0x05
#define FDC_DMA_MASK      0x0A
#define FDC_DMA_MODE      0x0B
#define FDC_DMA_CLEAR_FF  0x0C
#define FDC_DMA_CH2_PAGE  0x81

#define FDC_DMA_MODE_READ  0x46  // single / inc / read / ch2
#define FDC_DMA_MODE_WRITE 0x4A  // single / inc / write / ch2
#define FDC_DMA_MASK_CH2   0x06  // mask channel 2
#define FDC_DMA_UNMASK_CH2 0x02

// Pre-computed SECTOR_BUFFER (0xE000) low / high bytes for the DMA
// address-register pokes.  Hardcoded instead of ``SECTOR_BUFFER & 0xFF``
// because cc.py doesn't fold ``(EXTERN_SYMBOL >> 8) & 0xFF`` at compile
// time — it would emit four runtime instructions per ``out``.
#define FDC_SECTOR_BUFFER_LO 0x00
#define FDC_SECTOR_BUFFER_HI 0xE0

// fdc_irq_flag's storage lives in the file-scope asm() block at the
// bottom of this file (the same block that defines fdc_irq6_handler);
// asm_name keeps the bare symbol name so the ISR's ``mov byte
// [fdc_irq_flag], 1`` stays valid.
uint8_t fdc_irq_flag __attribute__((asm_name("fdc_irq_flag")));

// CHS scratch slots — fdc_lba_to_chs deposits the decoded coordinates
// here (uint8_t globals get cc.py's _g_ prefix; the bytes are scoped to
// this file).  cc.py only allows ``*ptr = val`` writes through
// out_register parameters, so the simplest path for "return three
// numbers" is module-state instead of an out_register pile-up.
uint8_t fdc_chs_cyl;
uint8_t fdc_chs_sector;
uint8_t fdc_chs_head;

void fdc_irq6_handler();
void fdc_install_irq();
void fdc_wait_irq();

// rtc_sleep_ms: drivers/rtc.asm.  CX = ms, busy-wait via PIT tick counter.
void rtc_sleep_ms(int milliseconds __attribute__((in_register("cx"))));

// fdc_send: send AL to the FDC data register once it's ready to accept
// one (RQM=1, DIO=0).  Preserves AX and DX so chained sends in
// fdc_issue_read_write can pass the same value through multiple times.
__attribute__((preserve_register("ax"))) __attribute__((preserve_register("dx")))
void fdc_send(int byte __attribute__((in_register("ax")))) {
    while ((kernel_inb(FDC_MSR) & (FDC_MSR_RQM | FDC_MSR_DIO)) != FDC_MSR_RQM) {
    }
    kernel_outb(FDC_DATA, byte);
}

// fdc_recv: pull one byte from the FDC data register once one is ready
// (RQM=1, DIO=1).  Clobbers AX, DX (matches the asm version's contract).
int fdc_recv() {
    while ((kernel_inb(FDC_MSR) & (FDC_MSR_RQM | FDC_MSR_DIO)) != (FDC_MSR_RQM | FDC_MSR_DIO)) {
    }
    return kernel_inb(FDC_DATA);
}

// fdc_drain_result: discard the 7 result bytes (ST0, ST1, ST2, C, H, R, N)
// the controller pushes back after READ / WRITE / SEEK completion.
void fdc_drain_result() {
    int i;
    i = 0;
    while (i < 7) {
        fdc_recv();
        i = i + 1;
    }
}

// fdc_sense_interrupt: SENSE INTERRUPT command.  Drains the 2-byte
// (ST0, PCN) result the controller produces in response.
void fdc_sense_interrupt() {
    fdc_send(FDC_CMD_SENSE_INT);
    fdc_recv();
    fdc_recv();
}

// fdc_dma_setup: program 8237 channel 2 for one 512 B transfer at
// SECTOR_BUFFER.  ``mode`` is FDC_DMA_MODE_READ or FDC_DMA_MODE_WRITE.
__attribute__((preserve_register("ax")))
void fdc_dma_setup(int mode __attribute__((in_register("ax")))) {
    kernel_outb(FDC_DMA_MASK, FDC_DMA_MASK_CH2);
    kernel_outb(FDC_DMA_CLEAR_FF, 0);
    kernel_outb(FDC_DMA_CH2_ADDR, FDC_SECTOR_BUFFER_LO);
    kernel_outb(FDC_DMA_CH2_ADDR, FDC_SECTOR_BUFFER_HI);
    kernel_outb(FDC_DMA_CLEAR_FF, 0);
    kernel_outb(FDC_DMA_CH2_COUNT, 0xFF);    // (512 - 1) low
    kernel_outb(FDC_DMA_CH2_COUNT, 0x01);    // (512 - 1) high
    kernel_outb(FDC_DMA_MODE, mode);
    kernel_outb(FDC_DMA_CH2_PAGE, 0);
    kernel_outb(FDC_DMA_MASK, FDC_DMA_UNMASK_CH2);
}

// fdc_lba_to_chs: split a 0-based LBA into 1.44 MB CHS coordinates.
// Output sector is 1-based per the FDC command format; cylinder and
// head are 0-based.  cc.py emits unsigned ``div``, which matches the
// asm version (and works because LBA fits in 16 bits for floppies).
// Results land in fdc_chs_cyl / fdc_chs_sector / fdc_chs_head — the
// only callers (fdc_read_sector / fdc_write_sector) read them right
// back, so the scratch globals are fine.
void fdc_lba_to_chs(int lba) {
    int track;
    fdc_chs_sector = (lba % FDC_SECTORS_PER_TRACK) + 1;
    track = lba / FDC_SECTORS_PER_TRACK;
    fdc_chs_head = track & 1;
    fdc_chs_cyl = track >> 1;
}

// fdc_seek: SEEK to (cylinder, head) on drive 0; completes via IRQ 6.
// HDS byte = (head << 2) | drive_number, with drive_number always 0.
void fdc_seek(int cylinder, int head) {
    fdc_irq_flag = 0;
    fdc_send(FDC_CMD_SEEK);
    fdc_send(head << 2);
    fdc_send(cylinder);
    fdc_wait_irq();
    fdc_sense_interrupt();
}

// fdc_issue_read_write: send the 9 parameter bytes for READ / WRITE.
// EOT (sector number again) is set to ``sector`` so the controller does
// a single-sector transfer; DTL is 0xFF and ignored when N>0.
void fdc_issue_read_write(int command, int cylinder, int sector, int head) {
    fdc_send(command);
    fdc_send(head << 2);
    fdc_send(cylinder);
    fdc_send(head);
    fdc_send(sector);
    fdc_send(FDC_SECTOR_SIZE_CODE);
    fdc_send(sector);
    fdc_send(FDC_GAP3);
    fdc_send(0xFF);
}

// fdc_init: one-time controller init.  Install IRQ 6 + unmask, reset
// the FDC, drain the four post-reset polling interrupts, set the
// 500 Kbps data rate, SPECIFY in DMA mode, motor 0 on (waiting half a
// second for spin-up), then recalibrate drive 0.  Called once from
// stage2's boot_shell, gated behind ``boot_disk < 0x80``.
void fdc_init() {
    fdc_install_irq();
    fdc_irq_flag = 0;

    // Reset: clear DOR (assert reset), then raise RESET_NOT with
    // DMA + IRQ enabled.
    kernel_outb(FDC_DOR, 0);
    kernel_outb(FDC_DOR, FDC_DOR_RESET_NOT | FDC_DOR_DMA_IRQ);
    fdc_wait_irq();

    // Drain the 4 polling interrupts (one per drive slot on 82077AA).
    fdc_sense_interrupt();
    fdc_sense_interrupt();
    fdc_sense_interrupt();
    fdc_sense_interrupt();

    // Data rate 500 Kbps for 1.44 MB.
    kernel_outb(FDC_CCR, 0);

    // SPECIFY: SRT/HUT don't matter on QEMU; HLT=1, ND=0 (DMA mode).
    fdc_send(FDC_CMD_SPECIFY);
    fdc_send(0xDF);
    fdc_send(0x02);

    // Motor 0 on, wait for spin-up.
    kernel_outb(FDC_DOR, FDC_DOR_MOTOR_0 | FDC_DOR_RESET_NOT | FDC_DOR_DMA_IRQ);
    rtc_sleep_ms(500);

    // Recalibrate drive 0.
    fdc_irq_flag = 0;
    fdc_send(FDC_CMD_RECALIBRATE);
    fdc_send(0);
    fdc_wait_irq();
    fdc_sense_interrupt();
}

// fdc_read_sector: fill SECTOR_BUFFER from disk at the given LBA.
// CF=0 on success (the asm version always reported success — there's
// no error path because IRQ 6 always eventually fires under QEMU; if
// real hardware drifted we'd hang in fdc_wait_irq).  AX = LBA in to
// match the contract block.c calls through.
__attribute__((carry_return))
__attribute__((preserve_register("bx"))) __attribute__((preserve_register("cx")))
__attribute__((preserve_register("dx"))) __attribute__((preserve_register("di")))
int fdc_read_sector(int lba __attribute__((in_register("ax")))) {
    fdc_lba_to_chs(lba);
    fdc_seek(fdc_chs_cyl, fdc_chs_head);
    fdc_dma_setup(FDC_DMA_MODE_READ);
    fdc_irq_flag = 0;
    fdc_issue_read_write(FDC_CMD_READ, fdc_chs_cyl, fdc_chs_sector, fdc_chs_head);
    fdc_wait_irq();
    fdc_drain_result();
    return 1;
}

// fdc_write_sector: write SECTOR_BUFFER to disk at the given LBA.
__attribute__((carry_return))
__attribute__((preserve_register("bx"))) __attribute__((preserve_register("cx")))
__attribute__((preserve_register("dx"))) __attribute__((preserve_register("di")))
int fdc_write_sector(int lba __attribute__((in_register("ax")))) {
    fdc_lba_to_chs(lba);
    fdc_seek(fdc_chs_cyl, fdc_chs_head);
    fdc_dma_setup(FDC_DMA_MODE_WRITE);
    fdc_irq_flag = 0;
    fdc_issue_read_write(FDC_CMD_WRITE, fdc_chs_cyl, fdc_chs_sector, fdc_chs_head);
    fdc_wait_irq();
    fdc_drain_result();
    return 1;
}

// fdc_irq6_handler: IRQ 6 fires on command completion for SEEK / RECAL /
// READ / WRITE.  We just flag it and EOI; the main path polls the flag.
//
// fdc_install_irq: install fdc_irq6_handler at IVT entry 0x26 (PIC
// remapped IRQ 6 to 0x26 in pic.asm; the BIOS default 0x0E is gone)
// and unmask IRQ 6 on the master PIC.  Bracketed by cli / sti so the
// IVT write is atomic with the mask change.
//
// fdc_wait_irq: block until IRQ 6 fires.  pushf / sti makes this safe
// to call from a syscall context (IF=0 on INT 30h entry) — popf
// restores the caller's IF state regardless.
asm("
fdc_irq6_handler:
        push ax
        mov byte [fdc_irq_flag], 1
        mov al, 0x20            ; PIC EOI
        out 0x20, al            ; PIC1 command port
        pop ax
        iret

fdc_install_irq:
        cli
        push ax
        push es
        xor ax, ax
        mov es, ax
        mov word [es:0x26*4], fdc_irq6_handler
        mov word [es:0x26*4 + 2], cs
        pop es
        in al, 0x21             ; PIC1 data port (IRQ mask)
        and al, 0xBF            ; clear bit 6 (IRQ 6 unmasked)
        out 0x21, al
        pop ax
        sti
        ret

fdc_wait_irq:
        pushf
        sti
.fdc_wait_loop:
        cmp byte [fdc_irq_flag], 0
        je .fdc_wait_loop
        mov byte [fdc_irq_flag], 0
        popf
        ret

fdc_irq_flag db 0
");
