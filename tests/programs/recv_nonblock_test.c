/* recv_nonblock_test — verify recvfrom returns AX=0 immediately when
   no matching packet is queued and SO_RCVTIMEO was NOT set.  Guards
   the default-0 non-blocking contract.  Prints OK on success,
   FAIL: <reason> otherwise.  Run via `tests/test_programs.py
   recv_nonblock_test`; the harness boots the OS with `-device
   ne2k_isa` (via with_net=True) so socket creation succeeds. */
int main() {
    int file_descriptor;
    char buffer[64];
    int received;
    int before;
    int after;
    file_descriptor = net_open(SOCK_DGRAM, IPPROTO_UDP);
    if (file_descriptor < 0) {
        printf("FAIL: socket\n");
        return 1;
    }
    before = uptime();
    received = recvfrom(file_descriptor, buffer, 64, 65000);
    after = uptime();
    close(file_descriptor);
    if (received != 0) {
        printf("FAIL: got bytes\n");
        return 1;
    }
    if (after != before) {
        printf("FAIL: blocked\n");
        return 1;
    }
    printf("OK\n");
    return 0;
}
