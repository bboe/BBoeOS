#define BACKGROUND_PALETTE_INDEX 0
#define COLUMNS 40
#define CURSOR_PALETTE_INDEX 15
#define ROWS 25

/* Standard VGA 16-colour palette (6-bit R/G/B), entries 0..14.  Matches
   the kernel's vga_default_palette exactly, so setting background_color_index
   to N makes the background look identical to a trail tile at palette index N. */
char background_blue[]  = {0, 42,  0, 42,  0, 42,  0, 42, 21, 63, 21, 63, 21, 63, 21};
char background_green[] = {0,  0, 42, 42,  0,  0, 21, 42, 21, 21, 63, 63, 21, 21, 63};
char background_red[]   = {0,  0,  0,  0, 42, 42, 42, 42, 21, 21, 21, 21, 63, 63, 63};

/* Forward declaration so trail_step (sorted alphabetically) can call
   trail_wrap defined below it. */
int trail_wrap(int value);

/* Cycle background_color_index by ±1 through the color table, skipping
   `avoid` (so it never collides with the current trail palette index). */
int background_step(int current, int step, int avoid) {
    int modulus = sizeof(background_red);
    int next = (current + step + modulus) % modulus;
    if (next == avoid) {
        next = (next + step + modulus) % modulus;
    }
    return next;
}

/* Cycle trail_palette_index by ±1 through {1..14}, skipping `avoid`. */
int trail_step(int current, int step, int avoid) {
    int next = trail_wrap(current + step);
    if (next == avoid) {
        next = trail_wrap(next + step);
    }
    return next;
}

/* Wrap a trail palette index into the usable range {BACKGROUND_PALETTE_INDEX+1
   .. CURSOR_PALETTE_INDEX-1} after a single-step advance. */
int trail_wrap(int value) {
    if (value == BACKGROUND_PALETTE_INDEX) {
        return CURSOR_PALETTE_INDEX - 1;
    }
    if (value == CURSOR_PALETTE_INDEX) {
        return BACKGROUND_PALETTE_INDEX + 1;
    }
    return value;
}

int main() {
    video_mode(VIDEO_MODE_VGA_320x200_256);
    int background_color_index = 0;
    char character = 0;
    int column = 0;
    int row = 0;
    int trail_palette_index = 1;
    set_palette_color(BACKGROUND_PALETTE_INDEX, background_red[background_color_index], background_green[background_color_index], background_blue[background_color_index]);
    fill_block(column, row, CURSOR_PALETTE_INDEX);
    while (character != 'q') {
        character = getchar();
        fill_block(column, row, trail_palette_index);
        if (character == 'a') {
            column = (column + COLUMNS - 1) % COLUMNS;
        } else if (character == 'd') {
            column = (column + 1) % COLUMNS;
        } else if (character == 's') {
            row = (row + 1) % ROWS;
        } else if (character == 'w') {
            row = (row + ROWS - 1) % ROWS;
        } else if (character == 'i') {
            trail_palette_index = trail_step(trail_palette_index, -1, background_color_index);
        } else if (character == 'o') {
            trail_palette_index = trail_step(trail_palette_index, 1, background_color_index);
        } else if (character == 'j') {
            background_color_index = background_step(background_color_index, -1, trail_palette_index);
            set_palette_color(BACKGROUND_PALETTE_INDEX, background_red[background_color_index], background_green[background_color_index], background_blue[background_color_index]);
        } else if (character == 'k') {
            background_color_index = background_step(background_color_index, 1, trail_palette_index);
            set_palette_color(BACKGROUND_PALETTE_INDEX, background_red[background_color_index], background_green[background_color_index], background_blue[background_color_index]);
        }
        fill_block(column, row, CURSOR_PALETTE_INDEX);
    }
    video_mode(VIDEO_MODE_TEXT_80x25);
}
