void main() {
    int dt[] = {0, 0, 0, 0, 0, 0, 0};
    datetime(dt);
    print_bcd(dt[0]);
    print_bcd(dt[1]);
    putc('-');
    print_bcd(dt[2]);
    putc('-');
    print_bcd(dt[3]);
    putc(' ');
    print_bcd(dt[4]);
    putc(':');
    print_bcd(dt[5]);
    putc(':');
    print_bcd(dt[6]);
    putc('\n');
}
