// arp.c -- ARP (Address Resolution Protocol)
//
// Public surface:
//   arp_handle_packet(frame): Process one Ethernet frame as ARP if applicable.
//                              Replies to ARP requests for our IP, caches replies.
//                              Called from icmp.c, udp.c, and arp_resolve.
//   arp_resolve(ip → mac):    Resolve IP to MAC, sending ARP request if not cached.
//                              Called from ip.c.
//
// Internal: 8-entry ARP table with round-robin eviction and 60-second TTL.

#define ARP_TABLE_SIZE 8
#define ARP_TTL_SECONDS 60

struct arp_entry {
    uint8_t ip[4];
    uint8_t mac[6];
    uint16_t timestamp;
};

struct arp_entry arp_table[ARP_TABLE_SIZE];
uint16_t arp_evict;

// our_ip / mac_address are asm-defined (ip.c's asm block plants our_ip;
// drivers/ne2k.asm plants mac_address).  asm_name aliases give us a
// uint8_t handle whose address is the start of the multi-byte block.
uint8_t our_ip_0 __attribute__((asm_name("our_ip")));
uint8_t mac_address_ref __attribute__((asm_name("mac_address")));

// uptime_seconds: seconds since boot (16-bit; wraps ~18h).  Defined in
// src/arch/x86/syscall.asm.
int uptime_seconds();

// ne2k_send / ne2k_receive: NIC primitives in drivers/ne2k.asm.
__attribute__((carry_return)) int ne2k_send(
    uint8_t *buffer __attribute__((in_register("si"))),
    int length __attribute__((in_register("cx"))));

__attribute__((carry_return)) int ne2k_receive(
    uint8_t *frame __attribute__((out_register("di"))),
    int *length __attribute__((out_register("cx"))));

// Forward declarations for helpers used before their definition.
__attribute__((carry_return)) int arp_table_lookup(
    uint8_t *ip __attribute__((in_register("si"))),
    uint8_t *mac_out __attribute__((out_register("di"))));

void arp_table_add(
    uint8_t *ip __attribute__((in_register("si"))),
    uint8_t *mac __attribute__((in_register("di"))));

__attribute__((carry_return)) int arp_send_request(
    uint8_t *target_ip __attribute__((in_register("si"))));

// arp_handle_packet: dispatch an ARP frame.  No return value.
void arp_handle_packet(uint8_t *frame __attribute__((in_register("si")))) {
    int opcode;
    uint8_t *txbuf;
    if (frame[12] != 0x08) { return; }
    if (frame[13] != 0x06) { return; }
    if (frame[20] != 0) { return; }
    opcode = frame[21];
    if (opcode == 2) {
        // ARP reply: cache sender (sender MAC at +22, sender IP at +28)
        arp_table_add(frame + 28, frame + 22);
        return;
    }
    if (opcode != 1) { return; }
    // ARP request: only respond if target IP matches ours
    if (memcmp(frame + 38, &our_ip_0, 4) != 0) { return; }
    arp_table_add(frame + 28, frame + 22);
    // Build reply at NET_TRANSMIT_BUFFER
    txbuf = NET_TRANSMIT_BUFFER;
    memcpy(txbuf, frame + 6, 6);             // dest = requester MAC
    memcpy(txbuf + 6, &mac_address_ref, 6);  // src = our MAC
    txbuf[12] = 0x08;
    txbuf[13] = 0x06;
    txbuf[14] = 0x00; txbuf[15] = 0x01;       // hwtype = Ethernet
    txbuf[16] = 0x08; txbuf[17] = 0x00;       // proto = IPv4
    txbuf[18] = 0x06;                         // hw size
    txbuf[19] = 0x04;                         // proto size
    txbuf[20] = 0x00; txbuf[21] = 0x02;       // opcode = reply
    memcpy(txbuf + 22, &mac_address_ref, 6);  // sender MAC
    memcpy(txbuf + 28, &our_ip_0, 4);         // sender IP
    memcpy(txbuf + 32, frame + 22, 6);        // target MAC = requester
    memcpy(txbuf + 38, frame + 28, 4);        // target IP = requester
    memset(txbuf + 42, 0, 18);                // pad to 60 bytes
    ne2k_send(txbuf, 60);
}

// arp_resolve: resolve target IP to MAC.  Returns CF clear with DI = MAC ptr
//   on success, CF set on timeout / send failure.
__attribute__((carry_return)) int arp_resolve(
    uint8_t *target_ip __attribute__((in_register("si"))),
    uint8_t *mac_out __attribute__((out_register("di")))) {
    int timeout;
    uint8_t *frame;
    int frame_len;
    uint8_t *found_mac;
    if (arp_table_lookup(target_ip, &found_mac)) {
        *mac_out = found_mac;
        return 1;
    }
    if (!arp_send_request(target_ip)) { return 0; }
    // Counter is decremented each iteration; ``!= 0`` (jne) avoids the
    // signed compare cc.py emits for ``> 0``, which would treat 0xFFFF
    // as -1 and exit the loop immediately.
    timeout = 0xFFFF;
    while (timeout != 0) {
        if (ne2k_receive(&frame, &frame_len)) {
            arp_handle_packet(frame);
            if (arp_table_lookup(target_ip, &found_mac)) {
                *mac_out = found_mac;
                return 1;
            }
        }
        timeout = timeout - 1;
    }
    return 0;
}

// arp_send_request: broadcast ARP "who has target_ip?"  CF set on send failure.
__attribute__((carry_return)) int arp_send_request(
    uint8_t *target_ip __attribute__((in_register("si")))) {
    uint8_t *txbuf;
    txbuf = NET_TRANSMIT_BUFFER;
    memset(txbuf, 0xFF, 6);                   // dest = broadcast
    memcpy(txbuf + 6, &mac_address_ref, 6);   // src = our MAC
    txbuf[12] = 0x08;
    txbuf[13] = 0x06;
    txbuf[14] = 0x00; txbuf[15] = 0x01;
    txbuf[16] = 0x08; txbuf[17] = 0x00;
    txbuf[18] = 0x06;
    txbuf[19] = 0x04;
    txbuf[20] = 0x00; txbuf[21] = 0x01;       // opcode = request
    memcpy(txbuf + 22, &mac_address_ref, 6);
    memcpy(txbuf + 28, &our_ip_0, 4);
    memset(txbuf + 32, 0, 6);                 // unknown target MAC
    memcpy(txbuf + 38, target_ip, 4);
    memset(txbuf + 42, 0, 18);                // pad to 60 bytes
    if (!ne2k_send(txbuf, 60)) { return 0; }
    return 1;
}

// arp_table_add: insert/update entry, evicting round-robin if full.
//   ip: pointer to 4 bytes; mac: pointer to 6 bytes.
void arp_table_add(
    uint8_t *ip __attribute__((in_register("si"))),
    uint8_t *mac __attribute__((in_register("di")))) {
    int i;
    int slot;
    struct arp_entry *entry;
    slot = -1;
    i = 0;
    while (i < ARP_TABLE_SIZE) {
        entry = &arp_table[i];
        if (entry->ip[0] == 0 && entry->ip[1] == 0
            && entry->ip[2] == 0 && entry->ip[3] == 0) {
            slot = i;
            break;
        }
        if (memcmp(entry->ip, ip, 4) == 0) {
            slot = i;
            break;
        }
        i = i + 1;
    }
    if (slot < 0) {
        slot = arp_evict;
        arp_evict = (arp_evict + 1) % ARP_TABLE_SIZE;
    }
    entry = &arp_table[slot];
    memcpy(entry->ip, ip, 4);
    memcpy(entry->mac, mac, 6);
    entry->timestamp = uptime_seconds();
}

// arp_table_lookup: return MAC for ip if cached and within TTL.
//   The asm version's "first empty slot ⇒ stop search" optimization is preserved
//   (entries are filled contiguously until eviction starts; once full, no slot
//   ever becomes empty again, so an empty slot mid-table genuinely means
//   "no further entries").
__attribute__((carry_return)) int arp_table_lookup(
    uint8_t *ip __attribute__((in_register("si"))),
    uint8_t *mac_out __attribute__((out_register("di")))) {
    int i;
    int age;
    struct arp_entry *entry;
    i = 0;
    while (i < ARP_TABLE_SIZE) {
        entry = &arp_table[i];
        if (entry->ip[0] == 0 && entry->ip[1] == 0
            && entry->ip[2] == 0 && entry->ip[3] == 0) {
            return 0;
        }
        if (memcmp(entry->ip, ip, 4) == 0) {
            // uptime_seconds() is called at the candidate-match site rather than
            // hoisted before the loop because cc.py auto-pins ``int now;`` to BX,
            // which the struct-deref base-register pattern (``mov bx, [bp-N]``)
            // then clobbers without invalidating the pin.  Inlining sidesteps
            // that bug; worst case is one extra uptime_seconds call per real hit.
            age = uptime_seconds() - entry->timestamp;
            if (age > ARP_TTL_SECONDS) { return 0; }
            *mac_out = entry->mac;
            return 1;
        }
        i = i + 1;
    }
    return 0;
}
