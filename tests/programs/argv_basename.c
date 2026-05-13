/* Smoke test for the Linux-style argv layout (argv[0] is the program
   basename, argc includes it, argv[argc] is NULL).  The shell stages
   the executed program's typed name (with any leading `bin/` stripped)
   into EXEC_ARG so the child's parse_argv writes argv[0] = "<name>".
   Tests/test_programs.py runs this with extra args ("alpha bravo") and
   matches the regex below. */
int main(int argc, char *argv[]) {
    /* Expect three space-separated tokens visible to the program:
         argv[0]="argv_basename"  argv[1]="alpha"  argv[2]="bravo"
       followed by a marker confirming argv[argc] is NULL. */
    printf("argc=%d\n", argc);
    int i = 0;
    while (i < argc) {
        printf("argv[%d]=%s\n", i, argv[i]);
        i += 1;
    }
    if (argv[argc] == NULL) {
        printf("argv[argc]=NULL\n");
    } else {
        printf("argv[argc]=NON-NULL (bug)\n");
    }
    return 0;
}
