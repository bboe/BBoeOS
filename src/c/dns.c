int decode_domain(char *base, int offset, char *out) {
    /* Decode DNS wire-format name starting at base+offset into dotted string.
       Follows compression pointers. Returns output length. */
    int label_count = 0;
    int out_position = 0;
    while (1) {
        char byte = base[offset];
        if (byte == '\0') {
            out[out_position] = '\0';
            return out_position;
        }
        if (byte >= '\xC0') {
            /* Compression pointer: next two bytes encode offset */
            char high = byte & 63;
            char low = base[offset + 1];
            offset = high * 256 + low;
        } else {
            /* Regular label */
            if (label_count > 0) {
                out[out_position] = '.';
                out_position = out_position + 1;
            }
            label_count = label_count + 1;
            offset = offset + 1;
            int length = byte;
            int copied = 0;
            while (copied < length) {
                out[out_position] = base[offset];
                out_position = out_position + 1;
                offset = offset + 1;
                copied = copied + 1;
            }
        }
    }
}

int encode_domain(char *domain, char *buf) {
    /* Encode dotted domain into DNS wire format (length-prefixed labels).
       Returns total encoded length, or 0 on error. */
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
    /* Skip a DNS wire-format name (labels or compression pointer).
       Returns offset past the name. */
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

char cname_buffer[128];
char dns_ip[4];
char name_buffer[128];
char query_buffer[512];

int main(int argc, char *argv[]) {
    if (argc != 1) {
        die("Usage: dns <domain>\n");
    }

    int error = mac(query_buffer);
    if (error) {
        die("No NIC found\n");
    }

    printf("Querying %s...\n", argv[0]);

    /* Header: ID=0x0001, Flags=0x0100 (RD), QDCOUNT=1 */
    query_buffer[0] = 0;
    query_buffer[1] = 1;
    query_buffer[2] = 1;
    query_buffer[3] = 0;
    query_buffer[4] = 0;
    query_buffer[5] = 1;
    query_buffer[6] = 0;
    query_buffer[7] = 0;
    query_buffer[8] = 0;
    query_buffer[9] = 0;
    query_buffer[10] = 0;
    query_buffer[11] = 0;

    /* Encode domain into QNAME starting at offset 12 */
    char *qname = query_buffer + 12;
    int name_length = encode_domain(argv[0], qname);
    if (name_length == 0) {
        die("DNS query failed\n");
    }

    /* QTYPE = A (0x0001), QCLASS = IN (0x0001) */
    char *after_name = qname + name_length;
    after_name[0] = 0;
    after_name[1] = 1;
    after_name[2] = 0;
    after_name[3] = 1;
    int query_length = 12 + name_length + 4;

    dns_ip[0] = 10;
    dns_ip[1] = 0;
    dns_ip[2] = 2;
    dns_ip[3] = 3;

    int socket_fd = net_open(SOCK_DGRAM, IPPROTO_UDP);
    if (socket_fd < 0) {
        die("DNS query failed\n");
    }
    int sent = sendto(socket_fd, query_buffer, query_length, dns_ip, 1024, 53);
    if (sent < 0) {
        close(socket_fd);
        die("DNS query failed\n");
    }

    int received = 0;
    int tries = 30000;
    while (tries > 0) {
        received = recvfrom(socket_fd, query_buffer, 512, 1024);
        if (received > 0) {
            break;
        }
        tries = tries - 1;
    }
    close(socket_fd);
    if (received == 0) {
        die("DNS query failed\n");
    }

    int answer_count = query_buffer[7];
    if (answer_count == 0) {
        die("No answer in DNS response\n");
    }

    /* Skip header (12) + question QNAME + QTYPE(2) + QCLASS(2) */
    int offset = skip_name(query_buffer, 12) + 4;

    int found_address = 0;

    /* Walk answer records */
    while (answer_count > 0) {
        int record_offset = offset;

        /* Skip RR name */
        offset = skip_name(query_buffer, offset);

        /* Read TYPE and RDLENGTH via a single base pointer */
        char *record = query_buffer + offset;
        int type_high = record[0];
        int type_low = record[1];
        int rdlength = record[9];

        /* Decode RR name (needed by both A and CNAME) */
        decode_domain(query_buffer, record_offset, name_buffer);

        if (type_high == 0 && type_low == 1) {
            /* A record */
            printf("%s is at ", name_buffer);
            char *ip_address = record + 10;
            print_ip(ip_address);
            putchar('\n');
            found_address = 1;
        } else if (type_high == 0 && type_low == 5) {
            /* CNAME record */
            decode_domain(query_buffer, offset + 10, cname_buffer);
            printf("%s is a CNAME for %s\n", name_buffer, cname_buffer);
        }
        offset = offset + 10 + rdlength;
        answer_count = answer_count - 1;
    }
    if (found_address == 0) {
        die("No answer in DNS response\n");
    }
    return 0;
}
