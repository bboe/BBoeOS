// ne2k.c — NE2000 ISA NIC driver (polled RX drain, IRQ 3 wake).
//
// Replaces drivers/ne2k.asm.  Same caller-facing surface:
//
//     ne2k_init                 setup; called by network_initialize
//     ne2k_probe        → CF    reset + read PROM MAC into mac_address
//     ne2k_receive      → CF    poll one frame; EDI = net_receive_buffer,
//                               ECX = packet length, CF set if no packet
//     ne2k_send (ESI,ECX) → CF  send one Ethernet frame
//     network_initialize → CF   probe + init wrapper; sets net_present
//
// `mac_address` (6 bytes) and `net_present` (1 byte) are referenced
// from net/arp.asm, net/ip.c, syscall/net.asm, and fs/fd/net.asm under
// their bare names; cc.py emits the storage as `_g_mac_address` /
// `_g_net_present`, so equ shims (below) alias the bare names back.
//
// I/O bases / register offsets inlined as bare integers — same rule as
// rtc.c / fdc.c (cc.py emits #define as %define which would clash with
// constants.asm's %assigns).  Reference table:
//   NE2K_BASE        = 0x300  (in src/include/constants.asm)
//   CR (command)     = +0x00   page 0/1 select, start/stop, DMA op
//   PSTART/PSTOP/BND = +0x01/+0x02/+0x03   RX ring start/end/boundary
//   TPSR/TBCR0/TBCR1 = +0x04/+0x05/+0x06   TX page start, byte count
//   ISR (page 0)     = +0x07   interrupt status (also CURR on page 1)
//   RSAR0/RSAR1      = +0x08/+0x09   remote DMA start address
//   RBCR0/RBCR1      = +0x0A/+0x0B   remote DMA byte count
//   RCR/TCR          = +0x0C/+0x0D   receive/transmit config
//   DCR              = +0x0E   data configuration (word-wide DMA)
//   IMR              = +0x0F   interrupt mask
//   DATA             = +0x10   remote DMA data port (16-bit)
//   RESET            = +0x1F   write-on-read triggers full reset

#include "registers.h"

#define NE2K_RX_START 0x46 // RX ring start page (after 6 TX pages)
#define NE2K_RX_STOP 0x80  // RX ring end page (one past last)
#define NE2K_TX_PAGE 0x40  // TX buffer start page

uint8_t mac_address[6];
uint8_t net_present;

// net_receive_buffer / net_transmit_buffer hold the kernel-virt of two
// 4 KB scratch frames (1.5 KB used inside each: a max-size Ethernet
// frame).  Allocated by `network_initialize` from the bitmap allocator
// only when the NIC is detected — systems booted without a NIC never
// spend the two frames.  asm callers (arp.asm, ip.c's inline asm,
// ne2k.c's own inline asm, the syscall path) load the pointer through
// the equ shims below: ``mov edi, [net_receive_buffer]``.
uint8_t *net_receive_buffer;
uint8_t *net_transmit_buffer;

// Bare-name aliases for asm callers (entry.asm sets net_present indirectly
// via network_initialize's return, but arp.asm/ip.c/syscall/net.asm read
// these directly under the bare names).
asm("mac_address equ _g_mac_address");
asm("net_present equ _g_net_present");
asm("net_receive_buffer equ _g_net_receive_buffer");
asm("net_transmit_buffer equ _g_net_transmit_buffer");

// Bring the NIC up for normal operation.  Must be called after a
// successful ne2k_probe.  No return value — the asm version was
// strictly init-or-trust-it-worked.
void ne2k_init() {
    int i;
    struct ne2k_cr cr_stop = {.rd = 4, .stop = 1};
    struct ne2k_cr cr_stop_page1 = {.page = 1, .rd = 4, .stop = 1};
    struct ne2k_rcr rcr = {.ab = 1};
    struct ne2k_tcr tcr = {0};
    struct ne2k_isr isr_ack_all = {.cnt = 1,
                                   .ovw = 1,
                                   .prx = 1,
                                   .ptx = 1,
                                   .rdc = 1,
                                   .rst = 1,
                                   .rxe = 1,
                                   .txe = 1};
    struct ne2k_imr imr = {.prx = 1};
    struct ne2k_cr cr_start = {.rd = 4, .start = 1};

    // Page 0, stop, abort DMA.
    kernel_outb(0x300, *(uint8_t *)&cr_stop);
    kernel_outb(0x300 + 0x01, NE2K_RX_START); // PSTART
    kernel_outb(0x300 + 0x02, NE2K_RX_STOP);  // PSTOP
    kernel_outb(0x300 + 0x03, NE2K_RX_START); // BOUNDARY
    kernel_outb(0x300 + 0x04, NE2K_TX_PAGE);  // TPSR

    // Page 1, stop, abort DMA.
    kernel_outb(0x300, *(uint8_t *)&cr_stop_page1);
    kernel_outb(0x300 + 0x07, NE2K_RX_START + 1); // CURR

    // Program PAR0..PAR5 with the MAC we read in ne2k_probe.
    i = 0;
    while (i < 6) {
        kernel_outb(0x300 + 0x01 + i, mac_address[i]);
        i = i + 1;
    }
    // Multicast filter MAR0..MAR7: accept all.
    i = 0;
    while (i < 8) {
        kernel_outb(0x300 + 0x08 + i, 0xFF);
        i = i + 1;
    }

    // Back to page 0, stop, abort DMA.
    kernel_outb(0x300, *(uint8_t *)&cr_stop);
    // RCR: accept broadcast.
    kernel_outb(0x300 + 0x0C, *(uint8_t *)&rcr);
    // TCR: normal mode (all fields zero: no loopback, no CRC inhibit).
    kernel_outb(0x300 + 0x0D, *(uint8_t *)&tcr);
    // ISR: clear all pending interrupts (ack by writing 1 to each bit).
    kernel_outb(0x300 + 0x07, *(uint8_t *)&isr_ack_all);
    // IMR: PRX (RX done) only; wakes hlt-parked sys_net_recvfrom via
    // pmode_irq3_handler in entry.asm.  Packet drain still happens in
    // process context via ne2k_receive.
    kernel_outb(0x300 + 0x0F, *(uint8_t *)&imr);
    // Page 0, start, abort DMA.
    kernel_outb(0x300, *(uint8_t *)&cr_start);
}

// Probe and reset the NIC, read the MAC PROM into mac_address.
// Returns 1 on success (CF clear), 0 if no NIC found / probe timed out.
int ne2k_probe() __attribute__((carry_return)) {
    int timeout;
    uint8_t status;
    uint16_t word;
    int i;
    uint8_t raw;
    struct ne2k_cr *cr_read;
    struct ne2k_isr *isr_read;
    struct ne2k_isr ack_all = {.cnt = 1,
                               .ovw = 1,
                               .prx = 1,
                               .ptx = 1,
                               .rdc = 1,
                               .rst = 1,
                               .rxe = 1,
                               .txe = 1};
    struct ne2k_cr cr_stop = {.rd = 4, .stop = 1};
    struct ne2k_dcr dcr = {.ft = 2, .ls = 1, .wts = 1};
    struct ne2k_rcr rcr_probe = {.mon = 1};
    struct ne2k_tcr tcr_probe = {.lb = 1};
    struct ne2k_cr cr_start_read = {.rd = 1, .start = 1};
    struct ne2k_isr ack_rdc = {.rdc = 1};

    // Pulse reset by reading then writing the reset port.
    status = kernel_inb(0x300 + 0x1F);
    kernel_outb(0x300 + 0x1F, status);

    // Wait up to ~64 KiB polls for ISR's RST bit.
    timeout = 0xFFFF;
    while (timeout > 0) {
        raw = kernel_inb(0x300 + 0x07);
        isr_read = (struct ne2k_isr *)&raw;
        if (isr_read->rst) {
            break;
        }
        timeout = timeout - 1;
    }
    if (timeout == 0) {
        return 0; // No NIC.
    }

    // Acknowledge all interrupts (ack by writing 1 to each bit).
    kernel_outb(0x300 + 0x07, *(uint8_t *)&ack_all);

    // Page 0, stop, abort DMA.
    kernel_outb(0x300, *(uint8_t *)&cr_stop);

    // Verify NIC presence by reading CR back.  The page bits (6-7) are
    // ignored; check only stop=1, start=0, transmit=0, rd=4 (bit 5 set).
    raw = kernel_inb(0x300);
    cr_read = (struct ne2k_cr *)&raw;
    if (cr_read->stop != 1 || cr_read->start != 0 || cr_read->transmit != 0 ||
        cr_read->rd != 4) {
        return 0;
    }

    // DCR: word-wide DMA (wts=1), normal byte order (bos=0), 16-bit DMA
    // (las=0), normal (ls=1), no auto-init (arm=0), 4-byte FIFO (ft=2).
    kernel_outb(0x300 + 0x0E, *(uint8_t *)&dcr);

    kernel_outb(0x300 + 0x0A, 0); // RBCR0
    kernel_outb(0x300 + 0x0B, 0); // RBCR1

    // RCR: monitor mode (no RX during probe).
    kernel_outb(0x300 + 0x0C, *(uint8_t *)&rcr_probe);
    // TCR: internal loopback.
    kernel_outb(0x300 + 0x0D, *(uint8_t *)&tcr_probe);

    // Set up a 32-byte remote-DMA read from PROM offset 0.
    kernel_outb(0x300 + 0x08, 0);    // RSAR0
    kernel_outb(0x300 + 0x09, 0);    // RSAR1
    kernel_outb(0x300 + 0x0A, 0x20); // RBCR0 = 32
    kernel_outb(0x300 + 0x0B, 0);    // RBCR1

    // CR: page 0, start, remote read DMA.
    kernel_outb(0x300, *(uint8_t *)&cr_start_read);

    // Word-mode DMA: each PROM byte is the low byte of a 16-bit read.
    i = 0;
    while (i < 6) {
        word = kernel_inw(0x300 + 0x10);
        mac_address[i] = word & 0xFF;
        i = i + 1;
    }
    // Drain the remaining 10 words to complete the 32-byte transfer.
    i = 0;
    while (i < 10) {
        kernel_inw(0x300 + 0x10);
        i = i + 1;
    }

    // Wait for remote DMA complete (ISR.rdc), then ack.
    raw = kernel_inb(0x300 + 0x07);
    isr_read = (struct ne2k_isr *)&raw;
    while (isr_read->rdc == 0) {
        raw = kernel_inb(0x300 + 0x07);
        isr_read = (struct ne2k_isr *)&raw;
    }
    kernel_outb(0x300 + 0x07, *(uint8_t *)&ack_rdc);
    return 1;
}

// ne2k_receive: poll the RX ring for one frame.  The body stays as one
// inline-asm block — byte-for-byte equivalent to the original
// drivers/ne2k.asm version — but the C declaration captures the multi-
// register return (EDI = net_receive_buffer pointer, ECX = packet
// length, CF = packet-available) via out_register parameters and
// carry_return so C callers see it as a normal function.
__attribute__((carry_return)) int
ne2k_receive(uint8_t *frame_pointer __attribute__((out_register("edi"))),
             int *length __attribute__((out_register("ecx"))));

asm("ne2k_receive:\n"
    "        push eax\n"
    "        push ebx\n"
    "        push edx\n"
    "        push esi\n"

    "        mov dx, 0x300\n" // CR
    "        mov al, 0x62\n"  // Page 1, start, abort DMA
    "        out dx, al\n"
    "        mov dx, 0x307\n" // CURR (page 1)
    "        in al, dx\n"
    "        mov bl, al\n"    // BL = CURR
    "        mov dx, 0x300\n" // CR
    "        mov al, 0x22\n"  // Page 0, start, abort DMA
    "        out dx, al\n"

    // Next read page = BOUNDARY + 1, wrapping at PSTOP.
    "        mov dx, 0x303\n" // BOUNDARY
    "        in al, dx\n"
    "        inc al\n"
    "        cmp al, 0x80\n" // NE2K_RX_STOP
    "        jb .ne2k_recv_no_wrap\n"
    "        mov al, 0x46\n" // NE2K_RX_START
    ".ne2k_recv_no_wrap:\n"
    "        cmp al, bl\n"
    "        je .ne2k_recv_empty\n"
    "        mov bh, al\n" // BH = read page

    // Read the 4-byte ring-buffer header via remote DMA.
    "        mov dx, 0x308\n" // RSAR0
    "        xor al, al\n"
    "        out dx, al\n"
    "        mov dx, 0x309\n" // RSAR1
    "        mov al, bh\n"
    "        out dx, al\n"
    "        mov dx, 0x30A\n" // RBCR0
    "        mov al, 4\n"
    "        out dx, al\n"
    "        mov dx, 0x30B\n" // RBCR1
    "        xor al, al\n"
    "        out dx, al\n"
    "        mov dx, 0x300\n" // CR
    "        mov al, 0x0A\n"  // Start + remote read DMA
    "        out dx, al\n"

    "        mov dx, 0x310\n" // Data
    "        in ax, dx\n"     // AL = status, AH = next page
    "        mov bl, ah\n"
    "        in ax, dx\n" // AX = total length (incl 4-byte header)
    "        sub ax, 4\n"
    "        movzx ecx, ax\n" // ECX = frame length

    "        mov dx, 0x307\n" // ISR
    ".ne2k_recv_wait_hdr:\n"
    "        in al, dx\n"
    "        test al, 0x40\n" // RDC
    "        jz .ne2k_recv_wait_hdr\n"
    "        mov al, 0x40\n"
    "        out dx, al\n"

    // Frame data: round count up to even, then word-mode DMA into net_receive_buffer.
    "        push ecx\n" // Save real length for ECX return.
    "        mov eax, ecx\n"
    "        inc eax\n"
    "        and eax, 0xFFFE\n"
    "        mov ecx, eax\n"

    "        mov dx, 0x308\n" // RSAR0
    "        mov al, 4\n"     // skip 4-byte header
    "        out dx, al\n"
    "        mov dx, 0x309\n" // RSAR1
    "        mov al, bh\n"
    "        out dx, al\n"
    "        mov dx, 0x30A\n" // RBCR0
    "        mov al, cl\n"
    "        out dx, al\n"
    "        mov dx, 0x30B\n" // RBCR1
    "        mov al, ch\n"
    "        out dx, al\n"
    "        mov dx, 0x300\n" // CR
    "        mov al, 0x0A\n"
    "        out dx, al\n"

    "        shr ecx, 1\n" // word count
    "        mov edi, [net_receive_buffer]\n"
    "        mov dx, 0x310\n"
    "        cld\n"
    "        rep insw\n"

    "        mov dx, 0x307\n" // ISR
    ".ne2k_recv_wait_pkt:\n"
    "        in al, dx\n"
    "        test al, 0x40\n"
    "        jz .ne2k_recv_wait_pkt\n"
    "        mov al, 0x40\n"
    "        out dx, al\n"

    // BOUNDARY = next_page - 1, wrapping at PSTART (≡ PSTOP-1 below PSTART).
    "        mov al, bl\n" // BL holds next page from header
    "        dec al\n"
    "        cmp al, 0x46\n" // NE2K_RX_START
    "        jae .ne2k_recv_bndy_ok\n"
    "        mov al, 0x7F\n" // NE2K_RX_STOP - 1
    ".ne2k_recv_bndy_ok:\n"
    "        mov dx, 0x303\n" // BOUNDARY
    "        out dx, al\n"

    "        pop ecx\n" // Restore frame length.
    "        mov edi, [net_receive_buffer]\n"
    "        clc\n"
    "        pop esi\n"
    "        pop edx\n"
    "        pop ebx\n"
    "        pop eax\n"
    "        ret\n"

    ".ne2k_recv_empty:\n"
    "        stc\n"
    "        pop esi\n"
    "        pop edx\n"
    "        pop ebx\n"
    "        pop eax\n"
    "        ret");

// Send one Ethernet frame.  ESI = frame pointer, ECX = length in bytes.
// Returns 1 on success (CF clear), 0 on timeout / TX error.  Pads short
// frames to the 60-byte minimum (NIC adds a 4-byte FCS for 64 on-wire).
int ne2k_send(uint8_t *frame __attribute__((in_register("esi"))),
              int length __attribute__((in_register("ecx"))))
    __attribute__((carry_return)) __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
    __attribute__((preserve_register("esi"))) {
    int dma_count;
    int word_count;
    uint8_t isr_raw;
    struct ne2k_isr *isr_read;
    int timeout;
    uint8_t had_txe;
    struct ne2k_cr cr_dma_write = {.rd = 2, .start = 1};
    struct ne2k_isr ack_rdc = {.rdc = 1};
    struct ne2k_cr cr_transmit = {.rd = 4, .start = 1, .transmit = 1};
    struct ne2k_isr ack_tx = {.ptx = 1, .txe = 1};

    if (length < 60) {
        length = 60;
    }
    dma_count = (length + 1) & 0xFFFE; // Round up to even (word DMA).
    word_count = dma_count >> 1;

    kernel_outb(0x300 + 0x08, 0);                       // RSAR0
    kernel_outb(0x300 + 0x09, NE2K_TX_PAGE);            // RSAR1
    kernel_outb(0x300 + 0x0A, dma_count & 0xFF);        // RBCR0
    kernel_outb(0x300 + 0x0B, (dma_count >> 8) & 0xFF); // RBCR1

    // CR: page 0, start, remote write DMA.
    kernel_outb(0x300, *(uint8_t *)&cr_dma_write);

    kernel_outsw(0x300 + 0x10, frame, word_count);

    // Wait for remote DMA complete (ISR.rdc), then ack.
    isr_raw = kernel_inb(0x300 + 0x07);
    isr_read = (struct ne2k_isr *)&isr_raw;
    while (isr_read->rdc == 0) {
        isr_raw = kernel_inb(0x300 + 0x07);
        isr_read = (struct ne2k_isr *)&isr_raw;
    }
    kernel_outb(0x300 + 0x07, *(uint8_t *)&ack_rdc); // Ack RDC.

    kernel_outb(0x300 + 0x04, NE2K_TX_PAGE);         // TPSR
    kernel_outb(0x300 + 0x05, length & 0xFF);        // TBCR0
    kernel_outb(0x300 + 0x06, (length >> 8) & 0xFF); // TBCR1

    // CR: page 0, start, transmit, abort DMA.
    kernel_outb(0x300, *(uint8_t *)&cr_transmit);

    timeout = 0xFFFF;
    isr_raw = 0;
    while (timeout > 0) {
        isr_raw = kernel_inb(0x300 + 0x07);
        isr_read = (struct ne2k_isr *)&isr_raw;
        if (isr_read->ptx || isr_read->txe) { // PTX or TXE
            break;
        }
        timeout = timeout - 1;
    }
    if (timeout == 0) {
        // Timeout.  Skip the PTX|TXE ack — matches the asm version, which
        // returns CF=1 without touching ISR so the next caller sees the
        // pending state and can decide how to recover.
        return 0;
    }
    // Save the error flag before the ack write.
    isr_read = (struct ne2k_isr *)&isr_raw;
    had_txe = isr_read->txe;
    // Ack PTX | TXE.
    kernel_outb(0x300 + 0x07, *(uint8_t *)&ack_tx);
    if (had_txe) {
        return 0; // TX error reported.
    }
    return 1;
}

// network_initialize: probe + init wrapper.  CF clear if NIC came up,
// CF set if no NIC was found (callers - currently only entry.asm -
// soldier on; net programs surface "no NIC" via net_present).
//
// Allocates one 4 KB NIC scratch frame on a successful probe and
// hands out per-buffer slices of it to the four named pointers
// (net_receive_buffer, net_transmit_buffer, arp_table, udp_buffer).
// The previous design used a separate frame per buffer, which paid
// 16 KB of RAM for ~3.4 KB of actual data on every NIC-present boot;
// packing drops that to one frame (4 KB).  Boot-without-NIC sessions
// still spend zero frames.
//
// Frame layout (4 KB total, 3.4 KB used, ~660 B unused):
//   0..1535   net_receive_buffer  (1.5 KB, max Ethernet frame)
//   1536..3071  net_transmit_buffer  (1.5 KB)
//   3072..3167  arp_table  (96 B; zero-filled below — lookup/add key on [entry] == 0)
//   3168..3431  udp_buffer  (264 B; overwritten on each send, no zero-fill)
//
// The allocation + offset assignments are tucked into a single
// inline-asm block because cc.py doesn't have a pointer cast syntax
// for the int-to-`uint8_t *` conversion.
int network_initialize() __attribute__((carry_return));
asm("network_initialize:\n"
    "        push eax\n"
    "        push ebx\n"
    "        push ecx\n"
    "        push edi\n"
    "        call ne2k_probe\n"
    "        jc .ni_no_nic\n"
    "        call frame_alloc\n"
    "        jc .ni_oom\n"
    "        add eax, DIRECT_MAP_BASE\n"
    // EAX = NIC scratch frame base (kernel-virt).  Slice into the four
    // named pointers; offsets must match the frame layout above.
    "        mov [_g_net_receive_buffer], eax\n"
    "        lea ebx, [eax + 1536]\n"
    "        mov [_g_net_transmit_buffer], ebx\n"
    "        lea ebx, [eax + 3072]\n"
    "        mov [arp_table], ebx\n"
    "        lea ebx, [eax + 3168]\n"
    "        mov [udp_buffer], ebx\n"
    // Zero the ARP-table slice (96 B) — frame_alloc returns a dirty
    // page and the lookup/add paths key on `[entry] == 0` for empty
    // slots.  The other slices are overwritten before they're read
    // (NE2000 hardware fills RX, ARP / IP / UDP code fills TX and
    // udp_buffer in full on each use).
    "        mov edi, [arp_table]\n"
    "        xor eax, eax\n"
    "        mov ecx, 24\n"
    "        cld\n"
    "        rep stosd\n"
    "        call ne2k_init\n"
    "        mov byte [_g_net_present], 1\n"
    "        clc\n"
    "        pop edi\n"
    "        pop ecx\n"
    "        pop ebx\n"
    "        pop eax\n"
    "        ret\n"
    ".ni_oom:\n"
    ".ni_no_nic:\n"
    "        stc\n"
    "        pop edi\n"
    "        pop ecx\n"
    "        pop ebx\n"
    "        pop eax\n"
    "        ret\n");
