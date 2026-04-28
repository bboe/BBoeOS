// ip.c -- IP layer: checksum computation and packet send
//
// ip_checksum: Ones-complement 16-bit checksum over a buffer (asm; requires ADC)
//   Input: SI = data pointer, CX = length in bytes (must be even)
//   Output: AX = checksum (complemented, ready to store)
//
// ip_send: Send an IP packet wrapped in an Ethernet frame
//   Input: BX = pointer to 4-byte dest IP, AL = IP protocol, SI = payload, CX = payload length
//   Output: CF set on error (ARP timeout or NIC send failure)

// asm_name aliases into asm-defined data (all three live in the ip_checksum asm block below)
uint8_t our_ip_0 __attribute__((asm_name("our_ip")));
uint8_t our_ip_1 __attribute__((asm_name("our_ip+1")));
uint8_t our_ip_2 __attribute__((asm_name("our_ip+2")));
uint8_t gateway_ip_0 __attribute__((asm_name("gateway_ip")));
uint16_t ip_id __attribute__((asm_name("ip_id")));
uint8_t mac_address_ref __attribute__((asm_name("_g_mac_address")));

// arp_resolve: Resolve IP to MAC address (SI=ip, DI=mac; CF on failure)
__attribute__((carry_return)) int arp_resolve(uint8_t *ip __attribute__((in_register("si"))), uint8_t *mac __attribute__((out_register("di"))));

// ip_checksum: Ones-complement 16-bit checksum (SI=data, CX=byte_count; AX=checksum)
int ip_checksum(uint8_t *data __attribute__((in_register("si"))), int length __attribute__((in_register("cx"))));

// ne2k_send: Transmit an Ethernet frame (SI=buffer, CX=length; CF on error)
__attribute__((carry_return)) int ne2k_send(uint8_t *buffer __attribute__((in_register("si"))), int length __attribute__((in_register("cx"))));

// ip_send: Send an IP packet wrapped in an Ethernet frame
__attribute__((carry_return)) int ip_send(uint8_t *dest_ip __attribute__((in_register("bx"))), uint8_t *payload __attribute__((in_register("si"))), int payload_length __attribute__((in_register("cx"))), int protocol_ax __attribute__((in_register("ax")))) {
    uint8_t protocol;
    int total_len;
    uint8_t *route_ip;
    uint8_t *dest_mac;
    uint8_t *txbuf;
    int checksum;
    protocol = protocol_ax & 0xFF;
    // Subnet /24 check: same first 3 bytes → ARP to dest directly, else via gateway
    if (dest_ip[0] == our_ip_0 && dest_ip[1] == our_ip_1 && dest_ip[2] == our_ip_2) {
        route_ip = dest_ip;
    } else {
        route_ip = &gateway_ip_0;
    }
    if (!arp_resolve(route_ip, &dest_mac)) { return 0; }
    txbuf = NET_TRANSMIT_BUFFER;
    // Ethernet header (14 bytes)
    memcpy(txbuf, dest_mac, 6);
    memcpy(txbuf + 6, &mac_address_ref, 6);
    txbuf[12] = 0x08;
    txbuf[13] = 0x00;
    // IP header (20 bytes at offset 14)
    total_len = payload_length + 20;
    txbuf[14] = 0x45;
    txbuf[15] = 0;
    txbuf[16] = (total_len >> 8) & 0xFF;
    txbuf[17] = total_len & 0xFF;
    txbuf[18] = (ip_id >> 8) & 0xFF;
    txbuf[19] = ip_id & 0xFF;
    ip_id = ip_id + 1;
    txbuf[20] = 0x40;
    txbuf[21] = 0;
    txbuf[22] = 64;
    txbuf[23] = protocol;
    txbuf[24] = 0;
    txbuf[25] = 0;
    memcpy(txbuf + 26, &our_ip_0, 4);
    memcpy(txbuf + 30, dest_ip, 4);
    // Payload
    memcpy(txbuf + 34, payload, payload_length);
    // Compute and store IP header checksum (little-endian, matching ip_checksum's lodsw output)
    checksum = ip_checksum(txbuf + 14, 20);
    txbuf[24] = checksum & 0xFF;
    txbuf[25] = (checksum >> 8) & 0xFF;
    // Send the frame
    total_len = payload_length + 34;
    if (!ne2k_send(txbuf, total_len)) { return 0; }
    return 1;
}

asm("
ip_checksum:
        ;; Ones-complement 16-bit checksum over a buffer
        ;; Input: SI = data pointer, CX = length in bytes (must be even)
        ;; Output: AX = checksum (complemented, ready to store)
        ;; Uses ADC to fold carry — not expressible as pure 16-bit C.
        push bx
        push cx
        push si

        xor bx, bx
        shr cx, 1             ; Word count
        .cksum_loop:
        lodsw
        add bx, ax
        adc bx, 0             ; Fold carry
        loop .cksum_loop

        not bx
        mov ax, bx

        pop si
        pop cx
        pop bx
        ret

        ;; Variables (arp.asm references our_ip; all three are asm_name'd above)
        gateway_ip db 10, 0, 2, 2
        ip_id dw 1
        our_ip db 10, 0, 2, 15
");
