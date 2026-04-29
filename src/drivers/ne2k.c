// ne2k.c — NE2000 ISA NIC driver (polled mode).
//
// Replaces drivers/ne2k.asm.  Same caller-facing surface:
//
//     ne2k_init                 setup; called by network_initialize
//     ne2k_probe        → CF    reset + read PROM MAC into mac_address
//     ne2k_receive      → CF    poll one frame; EDI = NET_RECEIVE_BUFFER,
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

#define NE2K_RX_START 0x46    // RX ring start page (after 6 TX pages)
#define NE2K_RX_STOP  0x80    // RX ring end page (one past last)
#define NE2K_TX_PAGE  0x40    // TX buffer start page

uint8_t mac_address[6];
uint8_t net_present;

// Bare-name aliases for asm callers (entry.asm sets net_present indirectly
// via network_initialize's return, but arp.asm/ip.c/syscall/net.asm read
// these directly under the bare names).
asm("mac_address equ _g_mac_address");
asm("net_present equ _g_net_present");

// Bring the NIC up for normal operation.  Must be called after a
// successful ne2k_probe.  No return value — the asm version was
// strictly init-or-trust-it-worked.
void ne2k_init() {
    int i;

    kernel_outb(0x300, 0x21);              // Page 0, stop, abort DMA.
    kernel_outb(0x300 + 0x01, NE2K_RX_START);   // PSTART
    kernel_outb(0x300 + 0x02, NE2K_RX_STOP);    // PSTOP
    kernel_outb(0x300 + 0x03, NE2K_RX_START);   // BOUNDARY
    kernel_outb(0x300 + 0x04, NE2K_TX_PAGE);    // TPSR

    kernel_outb(0x300, 0x61);                   // Page 1, stop, abort DMA.
    kernel_outb(0x300 + 0x07, NE2K_RX_START + 1);   // CURR

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

    kernel_outb(0x300, 0x21);              // Page 0.
    kernel_outb(0x300 + 0x0C, 0x04);       // RCR: accept broadcast.
    kernel_outb(0x300 + 0x0D, 0);          // TCR: normal (no loopback).
    kernel_outb(0x300 + 0x07, 0xFF);       // ISR: clear pending.
    kernel_outb(0x300 + 0x0F, 0);          // IMR: no IRQs (polled).
    kernel_outb(0x300, 0x22);              // Page 0, start, abort DMA.
}

// Probe and reset the NIC, read the MAC PROM into mac_address.
// Returns 1 on success (CF clear), 0 if no NIC found / probe timed out.
int ne2k_probe() __attribute__((carry_return)) {
    int timeout;
    uint8_t status;
    uint16_t word;
    int i;

    // Pulse reset by reading then writing the reset port.
    status = kernel_inb(0x300 + 0x1F);
    kernel_outb(0x300 + 0x1F, status);

    // Wait up to ~64 KiB polls for ISR's RST bit.
    timeout = 0xFFFF;
    while (timeout > 0) {
        if ((kernel_inb(0x300 + 0x07) & 0x80) != 0) {
            break;
        }
        timeout = timeout - 1;
    }
    if (timeout == 0) {
        return 0;                      // No NIC.
    }

    kernel_outb(0x300 + 0x07, 0xFF);   // Acknowledge all interrupts.
    kernel_outb(0x300, 0x21);          // Page 0, stop, abort DMA.

    // Verify NIC presence by reading CR back.  Mask off page-select bits.
    if ((kernel_inb(0x300) & 0x3F) != 0x21) {
        return 0;
    }

    kernel_outb(0x300 + 0x0E, 0x49);   // DCR: word-wide DMA, 4-byte FIFO.
    kernel_outb(0x300 + 0x0A, 0);      // RBCR0
    kernel_outb(0x300 + 0x0B, 0);      // RBCR1
    kernel_outb(0x300 + 0x0C, 0x20);   // RCR: monitor mode (no RX during probe).
    kernel_outb(0x300 + 0x0D, 0x02);   // TCR: internal loopback.

    // Set up a 32-byte remote-DMA read from PROM offset 0.
    kernel_outb(0x300 + 0x08, 0);      // RSAR0
    kernel_outb(0x300 + 0x09, 0);      // RSAR1
    kernel_outb(0x300 + 0x0A, 0x20);   // RBCR0 = 32
    kernel_outb(0x300 + 0x0B, 0);      // RBCR1
    kernel_outb(0x300, 0x0A);          // CR: start + remote read DMA

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

    // Wait for remote DMA complete (RDC bit in ISR), then ack.
    while ((kernel_inb(0x300 + 0x07) & 0x40) == 0) {}
    kernel_outb(0x300 + 0x07, 0x40);
    return 1;
}

// ne2k_receive: poll the RX ring for one frame.  The body stays as one
// inline-asm block — byte-for-byte equivalent to the original
// drivers/ne2k.asm version — but the C declaration captures the multi-
// register return (EDI = NET_RECEIVE_BUFFER pointer, ECX = packet
// length, CF = packet-available) via out_register parameters and
// carry_return so C callers see it as a normal function.
__attribute__((carry_return))
int ne2k_receive(uint8_t *frame_pointer __attribute__((out_register("edi"))),
                 int *length __attribute__((out_register("ecx"))));

asm("ne2k_receive:\n"
    "        push eax\n"
    "        push ebx\n"
    "        push edx\n"
    "        push esi\n"

    "        mov dx, 0x300\n"           // CR
    "        mov al, 0x62\n"            // Page 1, start, abort DMA
    "        out dx, al\n"
    "        mov dx, 0x307\n"           // CURR (page 1)
    "        in al, dx\n"
    "        mov bl, al\n"              // BL = CURR
    "        mov dx, 0x300\n"           // CR
    "        mov al, 0x22\n"            // Page 0, start, abort DMA
    "        out dx, al\n"

    // Next read page = BOUNDARY + 1, wrapping at PSTOP.
    "        mov dx, 0x303\n"           // BOUNDARY
    "        in al, dx\n"
    "        inc al\n"
    "        cmp al, 0x80\n"            // NE2K_RX_STOP
    "        jb .ne2k_recv_no_wrap\n"
    "        mov al, 0x46\n"            // NE2K_RX_START
    ".ne2k_recv_no_wrap:\n"
    "        cmp al, bl\n"
    "        je .ne2k_recv_empty\n"
    "        mov bh, al\n"              // BH = read page

    // Read the 4-byte ring-buffer header via remote DMA.
    "        mov dx, 0x308\n"           // RSAR0
    "        xor al, al\n"
    "        out dx, al\n"
    "        mov dx, 0x309\n"           // RSAR1
    "        mov al, bh\n"
    "        out dx, al\n"
    "        mov dx, 0x30A\n"           // RBCR0
    "        mov al, 4\n"
    "        out dx, al\n"
    "        mov dx, 0x30B\n"           // RBCR1
    "        xor al, al\n"
    "        out dx, al\n"
    "        mov dx, 0x300\n"           // CR
    "        mov al, 0x0A\n"            // Start + remote read DMA
    "        out dx, al\n"

    "        mov dx, 0x310\n"           // Data
    "        in ax, dx\n"               // AL = status, AH = next page
    "        mov bl, ah\n"
    "        in ax, dx\n"               // AX = total length (incl 4-byte header)
    "        sub ax, 4\n"
    "        movzx ecx, ax\n"           // ECX = frame length

    "        mov dx, 0x307\n"           // ISR
    ".ne2k_recv_wait_hdr:\n"
    "        in al, dx\n"
    "        test al, 0x40\n"           // RDC
    "        jz .ne2k_recv_wait_hdr\n"
    "        mov al, 0x40\n"
    "        out dx, al\n"

    // Frame data: round count up to even, then word-mode DMA into NET_RECEIVE_BUFFER.
    "        push ecx\n"                // Save real length for ECX return.
    "        mov eax, ecx\n"
    "        inc eax\n"
    "        and eax, 0xFFFE\n"
    "        mov ecx, eax\n"

    "        mov dx, 0x308\n"           // RSAR0
    "        mov al, 4\n"               // skip 4-byte header
    "        out dx, al\n"
    "        mov dx, 0x309\n"           // RSAR1
    "        mov al, bh\n"
    "        out dx, al\n"
    "        mov dx, 0x30A\n"           // RBCR0
    "        mov al, cl\n"
    "        out dx, al\n"
    "        mov dx, 0x30B\n"           // RBCR1
    "        mov al, ch\n"
    "        out dx, al\n"
    "        mov dx, 0x300\n"           // CR
    "        mov al, 0x0A\n"
    "        out dx, al\n"

    "        shr ecx, 1\n"              // word count
    "        mov edi, NET_RECEIVE_BUFFER\n"
    "        mov dx, 0x310\n"
    "        cld\n"
    "        rep insw\n"

    "        mov dx, 0x307\n"           // ISR
    ".ne2k_recv_wait_pkt:\n"
    "        in al, dx\n"
    "        test al, 0x40\n"
    "        jz .ne2k_recv_wait_pkt\n"
    "        mov al, 0x40\n"
    "        out dx, al\n"

    // BOUNDARY = next_page - 1, wrapping at PSTART (≡ PSTOP-1 below PSTART).
    "        mov al, bl\n"              // BL holds next page from header
    "        dec al\n"
    "        cmp al, 0x46\n"            // NE2K_RX_START
    "        jae .ne2k_recv_bndy_ok\n"
    "        mov al, 0x7F\n"            // NE2K_RX_STOP - 1
    ".ne2k_recv_bndy_ok:\n"
    "        mov dx, 0x303\n"           // BOUNDARY
    "        out dx, al\n"

    "        pop ecx\n"                 // Restore frame length.
    "        mov edi, NET_RECEIVE_BUFFER\n"
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
    __attribute__((carry_return))
    __attribute__((preserve_register("eax")))
    __attribute__((preserve_register("ecx")))
    __attribute__((preserve_register("edx")))
    __attribute__((preserve_register("esi")))
{
    int dma_count;
    uint8_t isr;
    int timeout;

    if (length < 60) {
        length = 60;
    }
    dma_count = (length + 1) & 0xFFFE;     // Round up to even (word DMA).

    kernel_outb(0x300 + 0x08, 0);                 // RSAR0
    kernel_outb(0x300 + 0x09, NE2K_TX_PAGE);      // RSAR1
    kernel_outb(0x300 + 0x0A, dma_count & 0xFF);          // RBCR0
    kernel_outb(0x300 + 0x0B, (dma_count >> 8) & 0xFF);   // RBCR1
    kernel_outb(0x300, 0x12);              // CR: start + remote write DMA.

    kernel_outsw(0x300 + 0x10, frame, dma_count >> 1);

    while ((kernel_inb(0x300 + 0x07) & 0x40) == 0) {}     // RDC
    kernel_outb(0x300 + 0x07, 0x40);                      // Ack RDC.

    kernel_outb(0x300 + 0x04, NE2K_TX_PAGE);              // TPSR
    kernel_outb(0x300 + 0x05, length & 0xFF);             // TBCR0
    kernel_outb(0x300 + 0x06, (length >> 8) & 0xFF);      // TBCR1
    kernel_outb(0x300, 0x26);              // CR: start, transmit.

    timeout = 0xFFFF;
    isr = 0;
    while (timeout > 0) {
        isr = kernel_inb(0x300 + 0x07);
        if ((isr & 0x0A) != 0) {           // PTX (0x02) or TXE (0x08)
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
    kernel_outb(0x300 + 0x07, 0x0A);       // Ack PTX | TXE.
    if ((isr & 0x08) != 0) {
        return 0;                          // TX error reported.
    }
    return 1;
}

// network_initialize: probe + init wrapper.  CF clear if NIC came up,
// CF set if no NIC was found (callers - currently only entry.asm -
// soldier on; netinit / net programs surface "no NIC" via net_present).
int network_initialize() __attribute__((carry_return)) {
    if (ne2k_probe()) {
        ne2k_init();
        net_present = 1;
        return 1;
    }
    return 0;
}
