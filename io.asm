visual_bell:
        push ax
        push bx
        push cx
        push dx
        mov ax, 0B00h
        mov bx, 0004h          ; Border color = red
        int 10h
        mov ah, 86h
        xor cx, cx
        mov dx, 0C350h         ; 50,000 µs = 50ms
        int 15h
        mov ax, 0B00h
        xor bx, bx             ; Border color = black
        int 10h
        pop dx
        pop cx
        pop bx
        pop ax
        ret
