// udp.c -- UDP receive (polls one datagram) and send (wraps payload in IP)
//
// udp_receive: Receive a UDP datagram (polls once, handles ARP transparently)
//   Output: DI = payload pointer (within NET_RECEIVE_BUFFER),
//           CX = payload length,
//           CF set if no UDP packet available.
//   The asm version also reported BX = source port and SI = source IP,
//   but the syscall layer ignores them; dropped on the C port.
//
// udp_send: Send a UDP datagram via IP
//   Input: BX = pointer to 4-byte dest IP,
//          DI = source port, DX = dest port,
//          SI = payload pointer, CX = payload length;
//   Output: CF set on error.

// arp_handle_packet: ARP-handle one Ethernet frame transparently (SI=frame).
void arp_handle_packet(uint8_t *packet __attribute__((in_register("si"))));

// ip_send: Send an IP packet wrapped in an Ethernet frame.
__attribute__((carry_return)) int ip_send(
    uint8_t *dest_ip __attribute__((in_register("bx"))),
    uint8_t *payload __attribute__((in_register("si"))),
    int payload_length __attribute__((in_register("cx"))),
    int protocol __attribute__((in_register("ax"))));

// ne2k_receive: Poll the NIC for one frame
//   Output: DI = NET_RECEIVE_BUFFER, CX = frame length, CF set if no packet.
__attribute__((carry_return)) int ne2k_receive(
    uint8_t *frame __attribute__((out_register("di"))),
    int *length __attribute__((out_register("cx"))));

// File-scope buffer for assembling outgoing UDP datagrams (8-byte header + payload).
uint8_t udp_buffer[256];

// udp_receive: Receive a UDP datagram (polls once, handles ARP transparently)
__attribute__((carry_return)) int udp_receive(
    uint8_t *payload __attribute__((out_register("di"))),
    int *length __attribute__((out_register("cx")))) {
    uint8_t *frame;
    int frame_length;
    int udp_total;
    if (!ne2k_receive(&frame, &frame_length)) { return 0; }
    arp_handle_packet(frame);
    if (frame[12] != 0x08) { return 0; }
    if (frame[13] != 0x00) { return 0; }
    if (frame[23] != 17) { return 0; }
    udp_total = (frame[38] << 8) | frame[39];
    *length = udp_total - 8;
    *payload = frame + 42;
    return 1;
}

// udp_send: Send a UDP datagram via IP (protocol 17)
__attribute__((carry_return)) int udp_send(
    uint8_t *dest_ip __attribute__((in_register("bx"))),
    int source_port __attribute__((in_register("di"))),
    int dest_port __attribute__((in_register("dx"))),
    uint8_t *payload __attribute__((in_register("si"))),
    int payload_length __attribute__((in_register("cx")))) {
    int udp_total;
    udp_total = payload_length + 8;
    udp_buffer[0] = (source_port >> 8) & 0xFF;
    udp_buffer[1] = source_port & 0xFF;
    udp_buffer[2] = (dest_port >> 8) & 0xFF;
    udp_buffer[3] = dest_port & 0xFF;
    udp_buffer[4] = (udp_total >> 8) & 0xFF;
    udp_buffer[5] = udp_total & 0xFF;
    udp_buffer[6] = 0;
    udp_buffer[7] = 0;
    memcpy(udp_buffer + 8, payload, payload_length);
    if (!ip_send(dest_ip, udp_buffer, udp_total, 17)) { return 0; }
    return 1;
}
