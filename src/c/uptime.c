void main() {
    int seconds = uptime();
    printf("%d:%d:%d\n", seconds / 3600, seconds % 3600 / 60, seconds % 60);
}
