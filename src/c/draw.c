void main() {
    video_mode(VIDEO_MODE_EGA_320x200_16);
    int background = 0;
    int column = 0;
    int row = 0;
    printf("\e[38;5;3m\e[48;5;0m");
    char character = getc();
    while (character != 'q') {
        int moved = 0;
        if (character == 'a') {
            column = (column + 39) % 40;
            moved = 1;
        } else if (character == 'd') {
            column = (column + 1) % 40;
            moved = 1;
        } else if (character == 's') {
            row = (row + 1) % 25;
            moved = 1;
        } else if (character == 'w') {
            row = (row + 24) % 25;
            moved = 1;
        } else if (character == 'j') {
            background = (background + 15) % 16;
            printf("\e[48;5;%dm", background);
        } else if (character == 'k') {
            background = (background + 1) % 16;
            printf("\e[48;5;%dm", background);
        }
        if (moved) {
            printf("\e[%d;%dH*", row + 1, column + 1);
        }
        character = getc();
    }
    video_mode(VIDEO_MODE_TEXT_80x25);
}
