/* Smoke test for the Linux-style argv layout (argv[0] is the program
   basename, argc includes it, argv[argc] is NULL).  The shell hands
   exec() a NULL-terminated char** array; the kernel validates it
   under the shell's PD, then during the child's build re-walks it
   under that same PD and copies each string directly into the new
   program's stack frame through a kmap alias, writing argc / argv
   pointers / NULL / empty envp before iretd (Linux SysV i386 startup
   contract).  tests/test_programs.py runs this with extra args
   ("alpha bravo") and matches the regex below. */
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
