/* Header consumed by src/c/inctest.c to smoke-test cc.py's #include.
   No include guards because cc.py has no #ifndef/#endif — include
   only once per translation unit. */

#define INCTEST_MAGIC 3054

int inctest_square(int x) {
    return x * x;
}
