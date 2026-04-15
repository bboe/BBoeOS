#define COLOR_MASK 15
#define COLUMNS 40
#define ROWS 25

void main() {
    video_mode(VIDEO_MODE_EGA_320x200_16);
    int background = 0;
    int changed = 1;
    int column = 0;
    int row = 0;
    char character = 0;
    while (character != 'q') {
        if (changed) {
            printf("\e[38;5;3m\e[48;5;%dm\e[%d;%dH*", background, row + 1, column + 1);
        }
        character = getc();
        changed = 1;
        if (character == 'a') {
            column = (column + COLUMNS - 1) % COLUMNS;
        } else if (character == 'd') {
            column = (column + 1) % COLUMNS;
        } else if (character == 's') {
            row = (row + 1) % ROWS;
        } else if (character == 'w') {
            row = (row + ROWS - 1) % ROWS;
        } else if (character == 'j') {
            background = (background - 1) & COLOR_MASK;
        } else if (character == 'k') {
            background = (background + 1) & COLOR_MASK;
        } else {
            changed = 0;
        }
    }
    video_mode(VIDEO_MODE_TEXT_80x25);
}
