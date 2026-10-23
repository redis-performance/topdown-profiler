#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

__attribute__((noinline)) static uint64_t divide_chain(uint64_t iterations) {
    uint64_t value = UINT64_C(0x123456789abcdef0);
    for (uint64_t i = 0; i < iterations; ++i) {
        uint64_t divisor = (i & 1023) + 3;
        value += UINT64_C(0x9e3779b97f4a7c15) + i;
        __asm__ volatile("udiv %0, %0, %1" : "+r"(value) : "r"(divisor));
        value ^= value << 13;
    }
    return value;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    uint64_t iterations = strtoull(argv[1], NULL, 10);
    if (!iterations || iterations > 200000000) return 2;
    alarm(30);
    printf("%llu\n", (unsigned long long)divide_chain(iterations));
    return 0;
}
