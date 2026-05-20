        ;; ARP request frame (60 bytes minimum Ethernet frame)
        arp_frame:
        ;; Ethernet header
        db 0FFh, 0FFh, 0FFh, 0FFh, 0FFh, 0FFh ; Dest MAC (broadcast)
        times 6 db 0                            ; Src MAC (filled at runtime)
        db 08h, 06h                             ; EtherType: ARP
        ;; ARP payload
        db 00h, 01h                             ; Hardware type: Ethernet
        db 08h, 00h                             ; Protocol type: IPv4
        db 06h                                  ; Hardware address length
        db 04h                                  ; Protocol address length
        db 00h, 01h                             ; Opcode: ARP request
        times 6 db 0                            ; Sender MAC (filled at runtime)
        db 10, 0, 2, 15                         ; Sender IP: 10.0.2.15
        times 6 db 0                            ; Target MAC (unknown)
        db 10, 0, 2, 2                          ; Target IP: 10.0.2.2
        times 18 db 0                           ; Pad to 60 bytes
