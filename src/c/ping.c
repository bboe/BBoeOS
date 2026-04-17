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
            position = position + 1;
            count = 0;
        } else {
            buf[position] = ch;
            position = position + 1;
            count = count + 1;
        }
        index = index + 1;
    }
}

int skip_name(char *buf, int offset) {
    while (1) {
        char byte = buf[offset];
        if (byte == '\0') {
            return offset + 1;
        }
        if (byte >= 192) {
            return offset + 2;
        }
        offset = offset + 1 + byte;
    }
}

int resolve_dns(char *domain, char *target) {
    char *query = SECTOR_BUFFER;
    query[0] = 0;  query[1] = 1;
    query[2] = 1;  query[3] = 0;
    query[4] = 0;  query[5] = 1;
    query[6] = 0;  query[7] = 0;
    query[8] = 0;  query[9] = 0;
    query[10] = 0; query[11] = 0;
    int name_length = encode_domain(domain, query + 12);
    if (name_length == 0) {
        return 1;
    }
    char *after = query + 12 + name_length;
    after[0] = 0; after[1] = 1;
    after[2] = 0; after[3] = 1;
    int query_length = 16 + name_length;

    char *dns_ip = BUFFER + 8;
    dns_ip[0] = 10; dns_ip[1] = 0; dns_ip[2] = 2; dns_ip[3] = 3;

    int fd = net_open(SOCK_DGRAM, IPPROTO_UDP);
    if (fd < 0) {
        return 1;
    }
    if (sendto(fd, query, query_length, dns_ip, 1024, 53) < 0) {
        close(fd);
        return 1;
    }

    int received = 0;
    int tries = 30000;
    while (tries > 0) {
        received = recvfrom(fd, query, 512, 1024);
        if (received > 0) {
            tries = 0;
        } else {
            tries = tries - 1;
        }
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
    while (answer_count > 0) {
        offset = skip_name(query, offset);
        char *record = query + offset;
        int rdlength = record[9];
        if (record[0] == 0 && record[1] == 1) {
            target[0] = record[10];
            target[1] = record[11];
            target[2] = record[12];
            target[3] = record[13];
            return 0;
        }
        offset = offset + 10 + rdlength;
        answer_count = answer_count - 1;
    }
    return 1;
}

int main(int argc, char *argv[]) {
    /* mac() writes into SECTOR_BUFFER to avoid clobbering argv, which
       lives inside BUFFER. */
    if (mac(SECTOR_BUFFER)) {
        die("No NIC found\n");
    }
    if (argc != 1) {
        die("Usage: ping <ip|hostname>\n");
    }
    char *target_ip = BUFFER;
    if (parse_ip(argv[0], target_ip)) {
        if (resolve_dns(argv[0], target_ip)) {
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

    char *packet = SECTOR_BUFFER;
    int seq = 1;
    int count = 4;
    while (count > 0) {
        packet[0] = 8;
        packet[1] = 0;
        packet[2] = 0;
        packet[3] = 0;
        packet[4] = 0;
        packet[5] = 1;
        packet[6] = seq / 256;
        packet[7] = seq;
        int i = 8;
        while (i < 16) {
            packet[i] = 0;
            i = i + 1;
        }
        int sum = checksum(packet, 16);
        packet[2] = sum;
        packet[3] = sum / 256;

        int t0 = ticks();
        int got = 0;
        if (sendto(fd, packet, 16, target_ip, 0, 0) > 0) {
            /* ~32k tries is plenty for the local ring to surface a reply
               and fits signed 16-bit, which is how our C subset compares. */
            int tries = 30000;
            while (tries > 0) {
                int n = recvfrom(fd, packet, 128, 0);
                if (n > 0 && packet[0] == 0) {
                    got = 1;
                    tries = 0;
                } else {
                    tries = tries - 1;
                }
            }
        }
        if (got) {
            printf("Reply from ");
            print_ip(target_ip);
            printf(": time=%d ticks\n", ticks() - t0);
        } else {
            printf("Request timed out\n");
        }
        sleep(1000);
        seq = seq + 1;
        count = count - 1;
    }
    close(fd);
}
