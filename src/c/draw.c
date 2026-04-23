#define COLUMNS 40
#define ROWS 25
#define CURSOR_COLOR 15

int main() {
    video_mode(VIDEO_MODE_VGA_320x200_256);
    int background = 1;
    int column = 0;
    int row = 0;
    char ch = 0;
    fill_block(column, row, CURSOR_COLOR);
    while (ch != 'q') {
        ch = getchar();
        fill_block(column, row, background);
        if (ch == 'a') {
            column = (column + COLUMNS - 1) % COLUMNS;
        } else if (ch == 'd') {
            column = (column + 1) % COLUMNS;
        } else if (ch == 's') {
            row = (row + 1) % ROWS;
        } else if (ch == 'w') {
            row = (row + ROWS - 1) % ROWS;
        } else if (ch == 'j') {
            background = (background + 15) & 15;
        } else if (ch == 'k') {
            background = (background + 1) & 15;
        }
        fill_block(column, row, CURSOR_COLOR);
    }
    video_mode(VIDEO_MODE_TEXT_80x25);
}
