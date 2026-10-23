#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/qos.h>

int main(int argc, char **argv) {
    if (argc != 3 && argc != 4) {
        fprintf(stderr, "Usage: counted-loop ITERATIONS default|background [--barrier]\n");
        return 2;
    }
    int barrier = argc == 4;
    if (barrier && strcmp(argv[3], "--barrier")) return 2;
    char *end = NULL;
    errno = 0;
    uint64_t iterations = strtoull(argv[1], &end, 10);
    if (errno || end == argv[1] || *end || iterations > 100000000) return 2;
    if (!strcmp(argv[2], "background")) {
        if (pthread_set_qos_class_self_np(QOS_CLASS_BACKGROUND, 0)) return 3;
    } else if (strcmp(argv[2], "default")) return 2;
    if (barrier) {
        puts("ready");
        fflush(stdout);
        if (getchar() != 's') return 5;
    }
    uint64_t remaining = iterations, checksum = 0;
    if (remaining) {
        __asm__ volatile(
            "1: add %0, %0, #1\n"
            "subs %1, %1, #1\n"
            "b.ne 1b\n"
            : "+r"(checksum), "+r"(remaining) :: "cc");
    }
    if (checksum != iterations || remaining) return 4;
    printf("{\"iterations\":%" PRIu64 ",\"checksum\":%" PRIu64 ",\"loop_instructions\":%" PRIu64 "}\n", iterations, checksum, iterations * 3);
    fflush(stdout);
    if (barrier && getchar() != 'e') return 6;
    return 0;
}
