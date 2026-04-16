int main() {
    char *mac_buffer = BUFFER;
    if (mac(mac_buffer)) {
        die("No NIC found\n");
    }
    memcpy(arp_frame + 6, mac_buffer, 6);
    memcpy(arp_frame + 22, mac_buffer, 6);

    int fd = net_open();
    if (fd < 0) {
        die("Socket error\n");
    }
    write(fd, arp_frame, 60);
    close(fd);
    die("ARP request sent\n");
}
