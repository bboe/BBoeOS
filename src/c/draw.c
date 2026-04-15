#define COLORS 16
#define COLUMNS 40
#define ROWS 25

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
            column = (column + COLUMNS - 1) % COLUMNS;
            moved = 1;
        } else if (character == 'd') {
            column = (column + 1) % COLUMNS;
            moved = 1;
        } else if (character == 's') {
            row = (row + 1) % ROWS;
            moved = 1;
        } else if (character == 'w') {
            row = (row + ROWS - 1) % ROWS;
            moved = 1;
        } else if (character == 'j') {
            background = (background + COLORS - 1) % COLORS;
            printf("\e[48;5;%dm", background);
        } else if (character == 'k') {
            background = (background + 1) % COLORS;
            printf("\e[48;5;%dm", background);
        }
        if (moved) {
            printf("\e[%d;%dH*", row + 1, column + 1);
        }
        character = getc();
    }
    video_mode(VIDEO_MODE_TEXT_80x25);
}
