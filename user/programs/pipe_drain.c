/* pipe_drain — reads a single byte from stdin then exits with status 9.

   Pairs with pipe_spam to exercise the "consumer closes while the
   producer is parked in kernel_yield_write" path that previously
   page-faulted the kernel.  The exit-after-one-byte pattern forces
   pipe_decrement_reader → pipe_wake_writer to fire while the
   producer's saved kernel context is still on its parked stack.
*/

int main() {
    char byte;
    read(STDIN, &byte, 1);
    return 9;
}
