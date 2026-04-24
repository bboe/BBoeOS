// icmp.c -- ICMP receive helper
//
// icmp_receive: Poll for one ICMP packet destined for us
//   Output: DI = pointer to ICMP bytes (within NET_RECEIVE_BUFFER),
//           CX = ICMP byte count, CF clear if packet received, CF set if none.
//   Assumes a 20-byte IP header (no IP options).

// ne2k_receive: Receive one Ethernet frame into NET_RECEIVE_BUFFER (DI=frame, CF if none)
__attribute__((carry_return)) int ne2k_receive(uint8_t *frame __attribute__((out_register("di"))));

// arp_handle_packet: Process a frame as ARP if applicable (SI=frame, no return value)
void arp_handle_packet(uint8_t *packet __attribute__((in_register("si"))));

// icmp_receive: Poll for one ICMP packet destined for us
__attribute__((carry_return)) int icmp_receive(uint8_t *icmp_data __attribute__((out_register("di"))), int *icmp_count __attribute__((out_register("cx")))) {
    uint8_t *frame;
    int total_len;
    if (!ne2k_receive(&frame)) { return 0; }
    arp_handle_packet(frame);
    if (frame[12] != 0x08) { return 0; }
    if (frame[13] != 0x00) { return 0; }
    if (frame[23] != 1) { return 0; }
    total_len = (frame[16] << 8) | frame[17];
    *icmp_count = total_len - 20;
    *icmp_data = frame + 34;
    return 1;
}
