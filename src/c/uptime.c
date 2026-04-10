void main() {
    int seconds = uptime();
    print_dec(seconds / 3600);
    putc(':');
    print_dec(seconds % 3600 / 60);
    putc(':');
    print_dec(seconds % 60);
    putc('\n');
}
