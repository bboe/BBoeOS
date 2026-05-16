;; Hand-crafted object-mode .asm exercising every CCREL_* marker.
;; Used by tests/test_ccobj.py to validate --pack-ccobj without
;; depending on cc.py's --object emission.
;;
;; Regenerate the matching .lst and .bin with:
;;     tests/data/ccobj/regen.sh

%include "ccobj_markers.inc"

section .text
global main
global helper

main:
    push ebp
    mov ebp, esp
    CCREL_CALL die
    CCREL_JMP _exit

helper:
    CCREL_MOVABS_LOAD_EAX errno
    CCREL_MOVABS_STORE_EAX errno
    ret

section .rodata
global format_string
format_string: db "hello", 0

section .data
global counter
counter: dd 42

section .bss
global scratch
scratch: resd 16
