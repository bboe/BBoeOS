	BITS 16

start:
	mov ax, 0x8e0		; Set up 4K stack space after this bootloader
	mov ss, ax		; (4096 + 512) / 16 bytes per paragraph
	mov sp, 4096
	mov ax, 0x07C0		; Set data segment to where we're loaded
	mov ds, ax


	mov si, welcome_string	; Put string position into SI
	call print_string	; Call our string-printing routine

	mov ah, 02h
	mov dh, 1
	int 10h

	mov si, version_string
	call print_string

	jmp $			; Jump here - infinite loop!
	

	welcome_string db 'Welcome to BBoeOS!', 0
	version_string db 'Version 0.0.1dev', 0


print_string:			; Routine: output string in SI to screen
	mov ah, 0Eh		; int 10h 'print char' function

	.repeat:
	lodsb			; Get character from string
	cmp al, 0
	je .done		; If char is zero, end of string
	int 10h			; Otherwise, print it
	jmp .repeat

	.done:
	ret


	times 510-($-$$) db 0	; Pad remainder of boot sector with 0s
	dw 0xAA55		; The standard PC boot signature
	