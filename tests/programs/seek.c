/* Smoke test for the seek() builtin / SYS_IO_SEEK kernel syscall.
 *
 * Opens src/macro_sm.asm (a known stable file on the boot image),
 * reads the first 32 bytes, then exercises each whence form:
 *   - SEEK_SET back to 0, re-read, expect identical bytes.
 *   - SEEK_CUR forward 16 then back 16, expect to land at offset 32.
 *   - SEEK_SET past EOF, expect the position to clamp to the file size
 *     and the next read to return 0 (EOF). */

int main() {
    char buf1[32];
    char buf2[32];
    int fd = open("src/macro_sm.asm", O_RDONLY);
    if (fd < 0) {
        die("seek: cannot open src/macro_sm.asm\n");
    }

    int n = read(fd, buf1, 32);
    if (n != 32) {
        die("seek: short initial read\n");
    }

    int pos = seek(fd, 0, SEEK_SET);
    if (pos != 0) {
        die("seek: SEEK_SET 0 returned wrong position\n");
    }
    n = read(fd, buf2, 32);
    if (n != 32) {
        die("seek: short read after SEEK_SET\n");
    }
    if (memcmp(buf1, buf2, 32) != 0) {
        die("seek: content after SEEK_SET differs\n");
    }

    pos = seek(fd, 16, SEEK_CUR);
    if (pos != 48) {
        die("seek: SEEK_CUR +16 wrong position\n");
    }
    pos = seek(fd, -16, SEEK_CUR);
    if (pos != 32) {
        die("seek: SEEK_CUR -16 wrong position\n");
    }

    pos = seek(fd, 1000000, SEEK_SET);
    if (pos != 1052) {
        die("seek: clamp past EOF wrong position\n");
    }
    n = read(fd, buf1, 32);
    if (n != 0) {
        die("seek: read past EOF returned data\n");
    }

    close(fd);
    printf("seek: OK\n");
}
