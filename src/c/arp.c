char mac_buffer[6];
char receive_buffer[128];
char target_ip[4];

int main(int argc, char *argv[]) {
    if (argc != 1) {
        die("usage: arp <ip>\n");
    }

    int error = parse_ip(argv[0], target_ip);
    if (error) {
        die("usage: arp <ip>\n");
    }

    error = mac(mac_buffer);
    if (error) {
        die("No NIC found\n");
    }

    memcpy(arp_frame + 6, mac_buffer, 6);
    memcpy(arp_frame + 22, mac_buffer, 6);
    memcpy(arp_frame + 38, target_ip, 4);

    int fd = net_open(SOCK_RAW, 0);
    if (fd < 0) {
        die("No NIC found\n");
    }

    write(fd, arp_frame, 60);

    int tries = 30000;
    while (tries > 0) {
        int bytes = read(fd, receive_buffer, 128);
        if (bytes > 0) {
            if (receive_buffer[12] == '\x08' && receive_buffer[13] == '\x06'
                && receive_buffer[20] == '\x00' && receive_buffer[21] == '\x02'
                && receive_buffer[28] == target_ip[0]
                && receive_buffer[29] == target_ip[1]
                && receive_buffer[30] == target_ip[2]
                && receive_buffer[31] == target_ip[3]) {
                close(fd);
                print_ip(target_ip);
                printf(" is at ");
                print_mac(receive_buffer + 22);
                putchar('\n');
                return 0;
            }
        }
        tries -= 1;
    }
    close(fd);
    die("ARP timeout\n");
}
