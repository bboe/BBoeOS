int main() {
    int rc = exec("cat");
    /* Expect rc == -ERROR_INVALID per Phase B (recursive exec rejected).
       printf %d prints unsigned; handle the negative sign manually. */
    if (rc < 0) {
        printf("rc=-%d\n", -rc);
    } else {
        printf("rc=%d\n", rc);
    }
    _exit(0);
}
