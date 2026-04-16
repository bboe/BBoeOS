int main() {
    int seconds = uptime();
    printf("%02d:%02d:%02d\n", seconds / 3600, seconds % 3600 / 60, seconds % 60);
}
