int encode_domain(char *domain, char *buf) {
    int label_start = 0;
    int position = 1;
    int count = 0;
    int index = 0;
    while (1) {
        char ch = domain[index];
        if (ch == '\0') {
            if (count == 0) {
                return 0;
            }
            buf[label_start] = count;
            buf[position] = 0;
            return position + 1;
        }
        if (ch == '.') {
            if (count == 0) {
                return 0;
            }
            buf[label_start] = count;
            label_start = position;
            position += 1;
            count = 0;
        } else {
            buf[position] = ch;
            position += 1;
            count += 1;
        }
        index += 1;
    }
}

int skip_name(char *buf, int offset) {
    while (1) {
        char byte = buf[offset];
        if (byte == '\0') {
            return offset + 1;
        }
        if (byte >= '\xC0') {
            return offset + 2;
        }
        offset = offset + 1 + byte;
    }
}

int resolve_dns(char *domain, char *target, char *query, char *ip) {
    /* DNS header: ID=0x0001, Flags=0x0100 (RD), QDCOUNT=1, rest zero. */
    memcpy(query, "\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00", 12);
    int name_length = encode_domain(domain, query + 12);
    if (name_length == 0) {
        return 1;
    }
    /* QTYPE=A, QCLASS=IN, both 0x0001 big-endian. */
    memcpy(query + 12 + name_length, "\x00\x01\x00\x01", 4);
    int query_length = 16 + name_length;

    memcpy(ip, "\x0a\x00\x02\x03", 4);

    int fd = net_open(SOCK_DGRAM, IPPROTO_UDP);
    if (fd < 0) {
        return 1;
    }
    if (sendto(fd, query, query_length, ip, 1024, 53) < 0) {
        close(fd);
        return 1;
    }

    int received = 0;
    int tries = 30000;
    while (tries) {
        received = recvfrom(fd, query, 512, 1024);
        if (received > 0) {
            break;
        }
        tries -= 1;
    }
    close(fd);
    if (received == 0) {
        return 1;
    }

    int answer_count = query[7];
    if (answer_count == 0) {
        return 1;
    }
    int offset = skip_name(query, 12) + 4;
    while (answer_count) {
        offset = skip_name(query, offset);
        char *record = query + offset;
        int rdlength = record[9];
        if (record[0] == 0 && record[1] == 1) {
            memcpy(target, record + 10, 4);
            return 0;
        }
        offset = offset + 10 + rdlength;
        answer_count -= 1;
    }
    return 1;
}

int main(int argc, char *argv[]) {
    char dns_ip[4];
    char packet_buffer[128];
    char query_buffer[512];
    char target_ip[4];
    if (mac(query_buffer)) {
        die("No NIC found\n");
    }
    if (argc != 1) {
        die("Usage: ping <ip|hostname>\n");
    }
    if (parse_ip(argv[0], target_ip)) {
        if (resolve_dns(argv[0], target_ip, query_buffer, dns_ip)) {
            die("Could not resolve hostname\n");
        }
    }
    printf("Pinging ");
    print_ip(target_ip);
    printf("...\n");

    int fd = net_open(SOCK_DGRAM, IPPROTO_ICMP);
    if (fd < 0) {
        die("Socket error\n");
    }

    int seq = 1;
    int count = 4;
    while (count) {
        /* ICMP echo request: type=8 code=0 checksum=placeholder
           identifier=0x0001 sequence=<seq>. Payload (bytes 8..15) is
           whatever happens to be in packet_buffer — echo reply mirrors
           it back verbatim. */
        memcpy(packet_buffer, "\x08\x00\x00\x00\x00\x01\x00\x00", 8);
        packet_buffer[7] = seq;
        int sum = checksum(packet_buffer, 16);
        packet_buffer[2] = sum;
        packet_buffer[3] = sum / 256;

        int start_time = uptime_ms();
        sendto(fd, packet_buffer, 16, target_ip, 0, 0);
        /* ~32k tries fits signed 16-bit (our C subset compares signed)
           and is plenty for the local ring to surface a reply. */
        int got = 0;
        int tries = 30000;
        while (tries) {
            int n = recvfrom(fd, packet_buffer, 128, 0);
            if (n > 0 && packet_buffer[0] == '\0') {
                got = 1;
                break;
            }
            tries -= 1;
        }
        if (got) {
            int duration = uptime_ms() - start_time;
            printf("Reply from ");
            print_ip(target_ip);
            printf(": time=%d ms\n", duration);
        } else {
            printf("Request timed out\n");
        }
        sleep(1000);
        seq += 1;
        count -= 1;
    }
    close(fd);
}
