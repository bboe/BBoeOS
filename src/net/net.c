// net.c -- Network protocol stack aggregator.
//
// Pulls in the four C-derived kasm files so the rest of the kernel can
// reference them via a single ``%include "net/net.kasm"``.
asm("%include \"net/arp.kasm\"");
asm("%include \"net/icmp.kasm\"");
asm("%include \"net/ip.kasm\"");
asm("%include \"net/udp.kasm\"");
