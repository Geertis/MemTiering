#define _GNU_SOURCE
#include <errno.h>
#include <linux/mempolicy.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Set before exec, including the interpreter's initial mappings. */
int main(int argc, char **argv)
{
    int mode;
    unsigned long mask;
    if (argc < 3) return 2;
    if (!strcmp(argv[1], "dram")) { mode = MPOL_BIND; mask = 1; }
    else if (!strcmp(argv[1], "cxl")) { mode = MPOL_BIND; mask = 2; }
    else if (!strcmp(argv[1], "static")) { mode = 6; mask = 3; }
    else return 2;
    if (syscall(SYS_set_mempolicy, mode, &mask, 64) < 0) {
        perror("set_mempolicy"); return 1;
    }
    execvp(argv[2], argv + 2);
    perror("execvp");
    return 1;
}
