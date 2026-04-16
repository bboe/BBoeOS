void main() {
    char *mac_buffer = BUFFER;
    int error = mac(mac_buffer);
    if (error) {
        die("No NIC found\n");
    }
    printf("NIC found: ");
    print_mac(mac_buffer);
    putc('\n');
}
