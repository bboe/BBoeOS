;; ccobj_markers.inc — marker macros for cc.py --object output.
;;
;; Each macro emits a fixed instruction encoding followed by a 4-byte
;; zero placeholder.  cc.py --pack-ccobj parses the NASM listing to
;; recover (section, offset, symbol, type) tuples for the linker.
;;
;; NASM never needs to know the macro arg is unresolved — the macro
;; emits raw bytes only.  The symbol name is recovered from the source
;; column of the listing.
;;
;; Patch offset within each macro's emitted bytes is always +1
;; (one byte of opcode, then the 4-byte placeholder).

%ifndef CCOBJ_MARKERS_INC
%define CCOBJ_MARKERS_INC

;; rel32 relocations — patch site is opcode+1, value is
;; symbol_address - (patch_site + 4).
%macro CCREL_CALL 1
    db 0xE8
    dd 0
%endmacro

%macro CCREL_JMP 1
    db 0xE9
    dd 0
%endmacro

;; abs32 relocations — patch site is opcode+1, value is symbol_address.
%macro CCREL_MOVABS_LOAD_EAX 1   ; mov eax, [%1]
    db 0xA1
    dd 0
%endmacro

%macro CCREL_MOVABS_STORE_EAX 1  ; mov [%1], eax
    db 0xA3
    dd 0
%endmacro

%endif
