int main() {
    char *argv[1];
    argv[0] = 0;
    int rc = exec("cat", argv);
    /* Expect rc == -ERROR_INVALID (recursive exec rejected by sys_exec).
       printf %d prints unsigned; handle the negative sign manually. */
    if (rc < 0) {
        printf("rc=-%d\n", -rc);
    } else {
        printf("rc=%d\n", rc);
    }
    _exit(0);
}
