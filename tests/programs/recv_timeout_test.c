/* recv_timeout_test — verify SO_RCVTIMEO causes recvfrom to block
   until the deadline and return AX=0.  Sets a 1500 ms timeout on an
   empty UDP socket, calls recvfrom, expects AX=0 and uptime() to
   have advanced by at least 1 second (the call DID block).  Prints
   OK on success, FAIL: <reason> otherwise. */
int main() {
    int file_descriptor;
    char buffer[64];
    int received;
    int before;
    int after;
    int set_result;
    file_descriptor = net_open(SOCK_DGRAM, IPPROTO_UDP);
    if (file_descriptor < 0) {
        printf("FAIL: socket\n");
        return 1;
    }
    set_result = setsockopt(file_descriptor, SO_RCVTIMEO, 1500);
    if (set_result != 0) {
        printf("FAIL: setsockopt\n");
        return 1;
    }
    before = uptime();
    received = recvfrom(file_descriptor, buffer, 64, 65001);
    after = uptime();
    close(file_descriptor);
    if (received != 0) {
        printf("FAIL: got bytes\n");
        return 1;
    }
    if (after - before < 1) {
        printf("FAIL: did not block\n");
        return 1;
    }
    if (after - before > 3) {
        printf("FAIL: blocked too long\n");
        return 1;
    }
    printf("OK\n");
    return 0;
}
