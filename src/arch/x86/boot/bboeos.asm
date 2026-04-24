        org 7C00h               ; offset where bios loads our first stage
        %include "constants.asm"
        %assign STAGE2_SECTORS (DIRECTORY_SECTOR - 1)

%include "stage1.asm"
%include "stage2.asm"
%include "kernel.asm"
