        org 7C00h               ; offset where bios loads our first stage
        %include "constants.asm"

%include "stage1.asm"
%include "stage2.asm"
%include "kernel.asm"

kernel_end:
