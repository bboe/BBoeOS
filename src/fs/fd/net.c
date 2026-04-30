// fd/net.c — read/write implementations for FD_TYPE_NET (raw NE2000
// sockets).  Dispatched via fd_ops in fs/fd.c when the syscall layer
// hands a NET-typed fd to fd_read / fd_write.
//
// Both functions match the asm-side ABI exactly: input ECX = byte
// count, output EAX = bytes copied / sent (or -1 on error), CF =
// error flag.  fd_write_net additionally reads its source buffer
// from the file-scope global `fd_write_buffer` (set by fd_write in
// fs/fd.c just before this is jumped to).

// drivers/ne2k.c — both declared with the asm-side multi-register
// contract.  ne2k_receive returns CF clear with EDI = NET_RECEIVE_BUFFER
// pointer and ECX = packet length; ne2k_send takes ESI = frame, ECX =
// length and returns CF set on error.
__attribute__((carry_return))
int ne2k_receive(uint8_t *frame_pointer __attribute__((out_register("edi"))),
                 int *length __attribute__((out_register("ecx"))));
__attribute__((carry_return))
int ne2k_send(uint8_t *frame __attribute__((in_register("esi"))),
              int length __attribute__((in_register("ecx"))));

// fs/fd.c file-scope global, set by fd_write before this function is
// reached.  Storage lives in fd.c; extern names the symbol without
// emitting a second copy.
extern uint8_t *fd_write_buffer;

// fd_read_net: poll the NIC for one frame; copy min(packet_length,
// max_bytes) into user_destination.  EAX = bytes copied (0 if no
// packet ready), CF clear throughout — matches the asm version's
// "no error" contract (a missing packet is a successful zero-byte
// read).
__attribute__((carry_return))
int fd_read_net(int *bytes_copied __attribute__((out_register("ax"))),
                uint8_t *user_destination __attribute__((in_register("edi"))),
                int max_bytes __attribute__((in_register("ecx")))) {
    uint8_t *frame_pointer;
    int packet_length;
    if (!ne2k_receive(&frame_pointer, &packet_length)) {
        *bytes_copied = 0;
        return 1;
    }
    if (packet_length > max_bytes) {
        packet_length = max_bytes;
    }
    memcpy(user_destination, frame_pointer, packet_length);
    *bytes_copied = packet_length;
    return 1;
}

// fd_write_net: send one raw Ethernet frame from `fd_write_buffer`.
// EAX = bytes sent on success, EAX = -1 / CF set on send failure.
__attribute__((carry_return))
int fd_write_net(int *bytes_sent __attribute__((out_register("ax"))),
                 int count __attribute__((in_register("ecx")))) {
    if (!ne2k_send(fd_write_buffer, count)) {
        *bytes_sent = -1;
        return 0;
    }
    *bytes_sent = count;
    return 1;
}
