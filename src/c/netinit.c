char mac_buffer[6];

int main() {
    int error = mac(mac_buffer);
    if (error) {
        die("No NIC found\n");
    }
    printf("NIC found: ");
    print_mac(mac_buffer);
    putchar('\n');
}
