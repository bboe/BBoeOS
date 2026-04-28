// ne2k.c — NE2000 ISA NIC driver (polled, port-mapped DMA).
//
// 16 KB of on-board RAM split into 64 pages of 256 bytes:
//     pages 0x40..0x45  TX buffer (6 pages = 1536 bytes — enough for
//                       the worst-case 1518-byte Ethernet frame).
//     pages 0x46..0x7F  RX ring buffer.
// All packet movement is via remote DMA — the CPU asks the NIC to set
// up a transfer and then reads / writes the data port (NE_DATA) to
// stream bytes in or out.  Polled mode (IRQ disabled): the receive
// path checks BOUNDARY vs. CURR; the send path waits on ISR's RDC /
// PTX bits.
//
// References:
//     Datasheet: https://media.digikey.com/pdf/Data%20Sheets/Texas%20Instruments%20PDFs/DP8390D,NS32490D.pdf
//     OSDev wiki: https://wiki.osdev.org/Ne2000
//
// Port assignments use full literals (NE_CR = 0x300 etc.) so the C
// #define-as-NASM-%define chain doesn't collide with the
// ``%assign NE2K_BASE 300h`` in include/constants.asm — same trap that
// has bitten ps2.c (PIC1_DATA), ansi.c (VGA_COLS), and fdc.c (DMA_*).
// The trade-off is that PAR0 / CURR / MAR0 (page-1 registers that
// alias page-0 PSTART / ISR / RSAR0) just get separate names rather
// than runtime page-index arithmetic.

// --- Page-0 registers ---
#define NE_CR     0x300  // Command Register
#define NE_PSTART 0x301  // Page Start (RX ring)
#define NE_PSTOP  0x302  // Page Stop  (RX ring)
#define NE_BNRY   0x303  // Boundary
#define NE_TPSR   0x304  // TX Page Start
#define NE_TBCR0  0x305  // TX Byte Count low
#define NE_TBCR1  0x306  // TX Byte Count high
#define NE_ISR    0x307  // Interrupt Status
#define NE_RSAR0  0x308  // Remote DMA address low
#define NE_RSAR1  0x309  // Remote DMA address high
#define NE_RBCR0  0x30A  // Remote DMA byte count low
#define NE_RBCR1  0x30B  // Remote DMA byte count high
#define NE_RCR    0x30C  // Receive Configuration
#define NE_TCR    0x30D  // Transmit Configuration
#define NE_DCR    0x30E  // Data Configuration
#define NE_IMR    0x30F  // Interrupt Mask
#define NE_DATA   0x310  // Data port (16-bit DMA window)
#define NE_RESET  0x31F  // Reset port

// --- Page-1 registers (reached by switching CR's PS bits) ---
#define NE_PAR0   0x301  // Physical Address 0 (MAC byte 0)
#define NE_CURR   0x307  // Current page (RX writer)
#define NE_MAR0   0x308  // Multicast Address 0

// --- CR command bytes ---
#define NE_CR_PG0_STOP  0x21  // Page 0, stop, abort DMA
#define NE_CR_PG0_START 0x22  // Page 0, start, abort DMA
#define NE_CR_PG1_STOP  0x61  // Page 1, stop, abort DMA
#define NE_CR_PG1_START 0x62  // Page 1, start, abort DMA
#define NE_CR_RD_READ   0x0A  // Page 0, start, remote read DMA
#define NE_CR_RD_WRITE  0x12  // Page 0, start, remote write DMA
#define NE_CR_TX        0x26  // Page 0, start, transmit

// --- ISR bits ---
#define NE_ISR_PTX 0x02  // Packet transmitted
#define NE_ISR_TXE 0x08  // Transmit error
#define NE_ISR_RDC 0x40  // Remote DMA complete
#define NE_ISR_RST 0x80  // Reset complete

// --- Ring buffer pages ---
#define NE_RX_START_PAGE 0x46  // 6 TX pages (0x40..0x45) precede the RX ring
#define NE_RX_STOP_PAGE  0x80  // one past the last RX page
#define NE_TX_PAGE       0x40  // first TX buffer page

// mac_address: 6-byte cached MAC, populated by ne2k_probe.  Plain C
// global — cc.py emits storage as ``_g_mac_address``.  arp.c / ip.c /
// syscalls.c reach this same byte run via ``asm_name("_g_mac_address")``
// aliases, since cc.py rejects ``asm_name`` on array declarations and
// has no proper extern keyword.
//
// net_present: single byte flipped to 1 by network_initialize after a
// successful probe.  Same _g_-prefixed alias scheme on the read side.
uint8_t mac_address[6];
uint8_t net_present;

// ne2k_init: program the NIC for normal operation.  Called once after
// a successful ne2k_probe.  Configures RX ring pages, TX buffer page,
// CURR write-cursor, programs PAR0..PAR5 from mac_address[], opens the
// multicast filter (accept all), masks interrupts (polled mode), and
// flips CR to start.
void ne2k_init() {
    int i;

    // Page 0, stop, DMA abort.
    kernel_outb(NE_CR, NE_CR_PG0_STOP);

    // Set up RX ring buffer pages.
    kernel_outb(NE_PSTART, NE_RX_START_PAGE);
    kernel_outb(NE_PSTOP, NE_RX_STOP_PAGE);
    kernel_outb(NE_BNRY, NE_RX_START_PAGE);

    // Set TX page start.
    kernel_outb(NE_TPSR, NE_TX_PAGE);

    // Switch to page 1 to set CURR and the physical address.
    kernel_outb(NE_CR, NE_CR_PG1_STOP);

    // CURR = next page NIC will write to (one past PSTART).
    kernel_outb(NE_CURR, NE_RX_START_PAGE + 1);

    // Program physical address registers PAR0..PAR5 from mac_address[].
    i = 0;
    while (i < 6) {
        kernel_outb(NE_PAR0 + i, mac_address[i]);
        i = i + 1;
    }

    // Multicast filter: accept all (MAR0..MAR7 = 0xFF).
    i = 0;
    while (i < 8) {
        kernel_outb(NE_MAR0 + i, 0xFF);
        i = i + 1;
    }

    // Switch back to page 0.
    kernel_outb(NE_CR, NE_CR_PG0_STOP);

    // RCR = 0x04: accept broadcast + unicast (no monitor / no multicast).
    kernel_outb(NE_RCR, 0x04);

    // TCR = 0: normal transmit mode (no loopback).
    kernel_outb(NE_TCR, 0x00);

    // Clear all pending interrupts.
    kernel_outb(NE_ISR, 0xFF);

    // IMR = 0: polled mode, no interrupts.
    kernel_outb(NE_IMR, 0x00);

    // Start the NIC.
    kernel_outb(NE_CR, NE_CR_PG0_START);
}

// ne2k_probe: reset the NIC, verify it responds, then read the 6-byte
// MAC from PROM via remote DMA (drains the rest of the 32-byte PROM
// window so the DMA channel ends in a clean state).  Returns CF clear
// on success / CF set on absence-or-timeout.
__attribute__((carry_return))
int ne2k_probe() {
    int timeout;
    int cr_value;
    int isr;
    int word;
    int i;

    // Reset: read the reset port and write the value back.
    kernel_outb(NE_RESET, kernel_inb(NE_RESET));

    // Wait for ISR.RST (bit 7) — gives up after 0xFFFF tries (which is
    // the asm version's "no NIC" timeout).
    timeout = 0xFFFF;
    while (timeout != 0) {
        if ((kernel_inb(NE_ISR) & NE_ISR_RST) != 0) { break; }
        timeout = timeout - 1;
    }
    if (timeout == 0) { return 0; }

    // Acknowledge all interrupts.
    kernel_outb(NE_ISR, 0xFF);

    // Stop the NIC: page 0, stop, abort DMA.
    kernel_outb(NE_CR, NE_CR_PG0_STOP);

    // Verify NIC by reading CR back (should match what we wrote modulo
    // the page-select bits — mask them off).
    cr_value = kernel_inb(NE_CR) & 0x3F;
    if (cr_value != NE_CR_PG0_STOP) { return 0; }

    // DCR = 0x49: word-wide DMA, normal mode, 4-byte FIFO.
    kernel_outb(NE_DCR, 0x49);

    // Clear remote byte count.
    kernel_outb(NE_RBCR0, 0x00);
    kernel_outb(NE_RBCR1, 0x00);

    // Monitor mode — don't accept packets during probe.
    kernel_outb(NE_RCR, 0x20);

    // Internal loopback for the probe transfer.
    kernel_outb(NE_TCR, 0x02);

    // Read 32 bytes of PROM via remote DMA: RSAR=0, RBCR=32.
    kernel_outb(NE_RSAR0, 0x00);
    kernel_outb(NE_RSAR1, 0x00);
    kernel_outb(NE_RBCR0, 0x20);
    kernel_outb(NE_RBCR1, 0x00);
    kernel_outb(NE_CR, NE_CR_RD_READ);

    // Read 6 MAC bytes (word mode: low byte of each 16-bit read).
    i = 0;
    while (i < 6) {
        word = kernel_inw(NE_DATA);
        mac_address[i] = word & 0xFF;
        i = i + 1;
    }

    // Drain the remaining 10 words to close out the 32-byte transfer.
    i = 0;
    while (i < 10) {
        kernel_inw(NE_DATA);
        i = i + 1;
    }

    // Wait for ISR.RDC (remote DMA complete), then ack.
    while (1) {
        isr = kernel_inb(NE_ISR);
        if ((isr & NE_ISR_RDC) != 0) { break; }
    }
    kernel_outb(NE_ISR, NE_ISR_RDC);

    return 1;
}

// ne2k_receive: poll the RX ring for one frame.  Returns DI =
// NET_RECEIVE_BUFFER (the asm contract — caller-visible buffer at a
// fixed kernel address) and CX = Ethernet frame length when a packet
// is available; CF set if the ring is empty.
//
// Two DMA transactions per packet: first a 4-byte ring-buffer header
// (status / next-page / 16-bit length) at the start of the read page,
// then the frame body itself starting four bytes in.  BOUNDARY is
// advanced to next_page-1 (with PSTART/PSTOP wrap) so the NIC can keep
// writing into the slots we've drained.
__attribute__((carry_return))
int ne2k_receive(uint8_t *frame __attribute__((out_register("di"))),
                 int *length __attribute__((out_register("cx")))) {
    int curr;
    int read_page;
    int next_page;
    int header_word;
    int frame_length;
    int word_count;
    int boundary;
    int isr;

    // Read CURR from page 1.
    kernel_outb(NE_CR, NE_CR_PG1_START);
    curr = kernel_inb(NE_CURR);
    kernel_outb(NE_CR, NE_CR_PG0_START);

    // Next read page = BOUNDARY + 1 (wrap at PSTOP).
    read_page = kernel_inb(NE_BNRY) + 1;
    if (read_page >= NE_RX_STOP_PAGE) { read_page = NE_RX_START_PAGE; }

    // If the next read page caught up with CURR, ring is empty.
    if (read_page == curr) { return 0; }

    // Read the 4-byte ring header via remote DMA: RSAR = read_page << 8.
    kernel_outb(NE_RSAR0, 0x00);
    kernel_outb(NE_RSAR1, read_page);
    kernel_outb(NE_RBCR0, 0x04);
    kernel_outb(NE_RBCR1, 0x00);
    kernel_outb(NE_CR, NE_CR_RD_READ);

    // Word 1: low byte = status, high byte = next page pointer.
    header_word = kernel_inw(NE_DATA);
    next_page = (header_word >> 8) & 0xFF;

    // Word 2: 16-bit total length (header + frame).
    frame_length = kernel_inw(NE_DATA) - 4;

    // Wait for header DMA complete, then ack.
    while (1) {
        isr = kernel_inb(NE_ISR);
        if ((isr & NE_ISR_RDC) != 0) { break; }
    }
    kernel_outb(NE_ISR, NE_ISR_RDC);

    // Now read the packet body.  Round byte count up to a word so the
    // word-mode DMA reads cleanly; we still report frame_length to the
    // caller (the trailing pad byte just sits in the buffer).
    word_count = ((frame_length + 1) & 0xFFFE) >> 1;

    kernel_outb(NE_RSAR0, 0x04);          // skip the 4-byte header
    kernel_outb(NE_RSAR1, read_page);
    kernel_outb(NE_RBCR0, ((frame_length + 1) & 0xFFFE) & 0xFF);
    kernel_outb(NE_RBCR1, (((frame_length + 1) & 0xFFFE) >> 8) & 0xFF);
    kernel_outb(NE_CR, NE_CR_RD_READ);

    kernel_insw(NE_DATA, NET_RECEIVE_BUFFER, word_count);

    while (1) {
        isr = kernel_inb(NE_ISR);
        if ((isr & NE_ISR_RDC) != 0) { break; }
    }
    kernel_outb(NE_ISR, NE_ISR_RDC);

    // Update BOUNDARY to next_page - 1 (wrap at PSTART going below).
    boundary = next_page - 1;
    if (boundary < NE_RX_START_PAGE) { boundary = NE_RX_STOP_PAGE - 1; }
    kernel_outb(NE_BNRY, boundary);

    *frame = NET_RECEIVE_BUFFER;
    *length = frame_length;
    return 1;
}

// ne2k_send: copy ``length`` bytes from ``buffer`` into the NIC's TX
// buffer page via remote write DMA, then issue the transmit command
// and wait (with timeout) for ISR.PTX or .TXE.  Pads short frames to
// the 60-byte Ethernet minimum.  Returns CF clear on successful
// transmit, CF set on TX error or timeout.
__attribute__((carry_return))
int ne2k_send(uint8_t *buffer __attribute__((in_register("si"))),
              int length __attribute__((in_register("cx")))) {
    int padded_length;
    int word_count;
    int byte_count;
    int isr;
    int timeout;

    if (length < 60) {
        padded_length = 60;
    } else {
        padded_length = length;
    }
    // Round byte count up to even for word-mode DMA.
    byte_count = (padded_length + 1) & 0xFFFE;
    word_count = byte_count >> 1;

    // Set remote DMA start address to the TX buffer (page << 8).
    kernel_outb(NE_RSAR0, 0x00);
    kernel_outb(NE_RSAR1, NE_TX_PAGE);

    // Set remote byte count.
    kernel_outb(NE_RBCR0, byte_count & 0xFF);
    kernel_outb(NE_RBCR1, (byte_count >> 8) & 0xFF);

    // Start remote write DMA.
    kernel_outb(NE_CR, NE_CR_RD_WRITE);

    // Stream the frame to the data port.
    kernel_outsw(NE_DATA, buffer, word_count);

    // Wait for remote DMA complete.
    while (1) {
        isr = kernel_inb(NE_ISR);
        if ((isr & NE_ISR_RDC) != 0) { break; }
    }
    kernel_outb(NE_ISR, NE_ISR_RDC);

    // Set TX page start + byte count.
    kernel_outb(NE_TPSR, NE_TX_PAGE);
    kernel_outb(NE_TBCR0, padded_length & 0xFF);
    kernel_outb(NE_TBCR1, (padded_length >> 8) & 0xFF);

    // Issue transmit command.
    kernel_outb(NE_CR, NE_CR_TX);

    // Wait for transmit complete or timeout.
    timeout = 0xFFFF;
    while (timeout != 0) {
        isr = kernel_inb(NE_ISR);
        if ((isr & (NE_ISR_PTX | NE_ISR_TXE)) != 0) { break; }
        timeout = timeout - 1;
    }
    if (timeout == 0) { return 0; }
    // Acknowledge whichever bit fired (PTX | TXE).
    kernel_outb(NE_ISR, NE_ISR_PTX | NE_ISR_TXE);
    if ((isr & NE_ISR_TXE) != 0) { return 0; }
    return 1;
}

// network_initialize: bring up the NIC.  Probes once, runs ne2k_init
// on success, and flips net_present so the syscall layer (sys_net_*
// in syscalls.c) can refuse net operations when there's no card.
__attribute__((carry_return))
int network_initialize() {
    if (!ne2k_probe()) { return 0; }
    ne2k_init();
    net_present = 1;
    return 1;
}

