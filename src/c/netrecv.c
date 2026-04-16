int main() {
    char *mac_buffer = BUFFER;
    int error = mac(mac_buffer);
    if (error) {
        die("No NIC found\n");
    }
    memcpy(arp_frame + 6, mac_buffer, 6);
    memcpy(arp_frame + 22, mac_buffer, 6);

    int fd = net_open();
    if (fd < 0) {
        die("Socket error\n");
    }
    write(fd, arp_frame, 60);
    printf("ARP sent, waiting for reply...\n");

    char *receive_buffer = BUFFER + 128;
    int bytes = 0;
    int tries = 30000;
    while (tries > 0) {
        bytes = read(fd, receive_buffer, 128);
        if (bytes > 0) {
            break;
        }
        tries = tries - 1;
    }
    close(fd);

    if (bytes == 0) {
        die("No reply (timeout)\n");
    }

    printf("Received: ");
    int i = 0;
    int limit = bytes;
    if (limit > 32) {
        limit = 32;
    }
    while (i < limit) {
        printf("%x ", receive_buffer[i]);
        i = i + 1;
    }
    putchar('\n');
}
