// syscalls.c — INT 30h handler bodies for the four non-trivial net_*
// syscalls.  The dispatcher (`syscall_handler`, `.iret_cf`, the dispatch
// table, `.check_shell`) and every other handler stay in
// `arch/x86/syscall.asm` because the trivial cases are 2-4 lines each
// — `call <existing_function>; jmp .iret_cf` — and gain nothing from a
// separate C wrapper.
//
// What lives here:
//   sys_net_mac         — copy cached 6-byte MAC into the caller's buffer
//   sys_net_open        — allocate a socket fd with the right FD_TYPE_*
//   sys_net_recvfrom    — fd-type dispatch into udp_receive / icmp_receive
//                         + UDP destination-port filter + payload memcpy
//   sys_net_sendto      — fd-type dispatch into udp_send / ip_send
//
// Each is called from the dispatcher with a plain `call sys_net_X; jmp
// .iret_cf`.  The return convention is cc.py's `carry_return` (return 1
// = success / CF clear, return 0 = error / CF set) plus
// `out_register("ax")` for the result the user sees in AX after iretd.
//
// `sys_net_sendto` receives the user's dst_port via AX — the dispatcher
// reads it from `[esp+8]` (the saved EBP slot in the pushad frame) and
// pre-loads it before the call, so the C body names it as a regular
// `in_register("ax")` parameter.

// fd table layout: only `type` and `flags` are touched here; the rest
// of the entry is opaque padding cc.py doesn't need to know about.
struct fd {
    uint8_t type;
    uint8_t flags;
    uint8_t _rest[30];
};

// fs/fd.c: returns AX = fd number, ESI = entry pointer, CF set on
// failure.
__attribute__((carry_return))
int fd_alloc(int *fd_num __attribute__((out_register("ax"))),
             struct fd *entry __attribute__((out_register("esi"))));
__attribute__((carry_return)) __attribute__((preserve_register("ecx")))
int fd_lookup(int fd_num __attribute__((in_register("bx"))),
              struct fd *entry __attribute__((out_register("esi"))));

// Network plumbing.  All four return CF set on error.
__attribute__((carry_return))
int udp_receive(uint8_t *payload __attribute__((out_register("edi"))),
                int *length __attribute__((out_register("ecx"))));
__attribute__((carry_return))
int udp_send(uint8_t *dest_ip __attribute__((in_register("ebx"))),
             int source_port __attribute__((in_register("edi"))),
             int dest_port __attribute__((in_register("edx"))),
             uint8_t *payload __attribute__((in_register("esi"))),
             int length __attribute__((in_register("ecx"))));
__attribute__((carry_return))
int icmp_receive(uint8_t *payload __attribute__((out_register("edi"))),
                 int *length __attribute__((out_register("ecx"))));
__attribute__((carry_return))
int ip_send(uint8_t *dest_ip __attribute__((in_register("ebx"))),
            uint8_t protocol __attribute__((in_register("ax"))),
            uint8_t *payload __attribute__((in_register("esi"))),
            int length __attribute__((in_register("ecx"))));

// drivers/ne2k.c file-scope globals.  extern names the symbols
// without emitting storage; the actual bytes live in ne2k.c.  The
// equ shims in ne2k.c expose the bare names for the asm callers in
// src/net/.
extern uint8_t net_present;
extern uint8_t mac_address[6];

// sys_net_mac: copy the 6-byte cached MAC into the caller's buffer at
// EDI.  CF set if no NIC was ever probed (net_present stays zero).
__attribute__((carry_return))
int sys_net_mac(uint8_t *out __attribute__((in_register("edi")))) {
    if (net_present == 0) { return 0; }
    memcpy(out, mac_address, 6);
    return 1;
}

// sys_net_open: allocate a socket fd with the right FD_TYPE_* tag.
//   AL = type (SOCK_RAW=0, SOCK_DGRAM=1)
//   DL = protocol (IPPROTO_ICMP / IPPROTO_UDP for SOCK_DGRAM; 0 for raw)
// On success AX = fd, CF clear.  On failure AX = -1, CF set.
__attribute__((carry_return))
int sys_net_open(int *result_fd __attribute__((out_register("ax"))),
                 int sock_type __attribute__((in_register("ax"))),
                 int protocol __attribute__((in_register("dx")))) {
    int fd_num;
    struct fd *entry;
    int type;
    int proto;
    type = sock_type & 0xFF;
    proto = protocol & 0xFF;
    if (net_present == 0) { *result_fd = -1; return 0; }
    if (!fd_alloc(&fd_num, &entry)) { *result_fd = -1; return 0; }
    if (type == SOCK_DGRAM) {
        if (proto == IPPROTO_ICMP) {
            entry->type = FD_TYPE_ICMP;
        } else {
            entry->type = FD_TYPE_UDP;
        }
    } else {
        entry->type = FD_TYPE_NET;
    }
    entry->flags = 0;
    *result_fd = fd_num;
    return 1;
}

// sys_net_recvfrom: dispatch by fd type.  UDP filters incoming packets
// against the caller's local port (passed in DX, host byte order); ICMP
// hands every received packet through.  Always returns CF clear; AX =
// bytes copied (0 if nothing matched).
__attribute__((carry_return))
int sys_net_recvfrom(int *bytes_copied __attribute__((out_register("ax"))),
                     int fd_num __attribute__((in_register("bx"))),
                     uint8_t *user_buffer __attribute__((in_register("edi"))),
                     int max_bytes __attribute__((in_register("ecx"))),
                     int local_port __attribute__((in_register("dx")))) {
    struct fd *entry;
    uint8_t *payload;
    int payload_length;
    int dest_port;
    uint8_t *receive_buffer;
    if (!fd_lookup(fd_num, &entry)) { *bytes_copied = 0; return 1; }
    if (entry->type == FD_TYPE_UDP) {
        if (!udp_receive(&payload, &payload_length)) { *bytes_copied = 0; return 1; }
        // UDP dest port lives at NET_RECEIVE_BUFFER+36 (big-endian).
        receive_buffer = NET_RECEIVE_BUFFER;
        dest_port = (receive_buffer[36] << 8) | receive_buffer[37];
        if (dest_port != (local_port & 0xFFFF)) { *bytes_copied = 0; return 1; }
    } else if (entry->type == FD_TYPE_ICMP) {
        if (!icmp_receive(&payload, &payload_length)) { *bytes_copied = 0; return 1; }
    } else {
        *bytes_copied = 0;
        return 1;
    }
    if (payload_length > max_bytes) { payload_length = max_bytes; }
    memcpy(user_buffer, payload, payload_length);
    *bytes_copied = payload_length;
    return 1;
}

// sys_net_sendto: dispatch by fd type.  UDP wraps the payload in
// udp_send (which adds the IP header); ICMP hands the bytes straight to
// ip_send with protocol = 1.  AX on entry holds the user's saved-EBP
// slot value (dst_port for UDP) — the dispatcher pre-loads it from
// [esp+8].  AX on success = bytes_sent; CF set on error (bad fd,
// unsupported type, send failure).
__attribute__((carry_return))
int sys_net_sendto(int *bytes_sent __attribute__((out_register("ax"))),
                   int dst_port __attribute__((in_register("ax"))),
                   int fd_num __attribute__((in_register("bx"))),
                   uint8_t *payload __attribute__((in_register("esi"))),
                   int payload_length __attribute__((in_register("ecx"))),
                   uint8_t *dest_ip __attribute__((in_register("edi"))),
                   int source_port __attribute__((in_register("dx")))) {
    struct fd *entry;
    if (!fd_lookup(fd_num, &entry)) { *bytes_sent = 0; return 0; }
    if (entry->type == FD_TYPE_UDP) {
        if (!udp_send(dest_ip, source_port, dst_port & 0xFFFF, payload, payload_length)) {
            *bytes_sent = 0; return 0;
        }
        *bytes_sent = payload_length;
        return 1;
    }
    if (entry->type == FD_TYPE_ICMP) {
        if (!ip_send(dest_ip, IPPROTO_ICMP, payload, payload_length)) {
            *bytes_sent = 0; return 0;
        }
        *bytes_sent = payload_length;
        return 1;
    }
    *bytes_sent = 0;
    return 0;
}
