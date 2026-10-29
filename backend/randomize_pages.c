#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <linux/mempolicy.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define BATCH_PAGES 4096

struct counters {
    uint64_t considered, present;
    uint64_t before_fast, before_slow;
    uint64_t target_fast, target_slow;
    uint64_t after_fast, after_slow;
    uint64_t moved, failed, unexpected;
};

static uint64_t splitmix64(uint64_t value)
{
    value += UINT64_C(0x9e3779b97f4a7c15);
    value = (value ^ (value >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)) * UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

static int target_node(uint64_t page_index, uint64_t seed, unsigned fast_percent,
                       int fast_node, int slow_node)
{
    return splitmix64(page_index ^ seed) % 100 < fast_percent
               ? fast_node : slow_node;
}

static long move_pages_call(pid_t pid, unsigned long count, void **pages,
                            const int *nodes, int *status, int flags)
{
    return syscall(SYS_move_pages, pid, count, pages, nodes, status, flags);
}

static bool eligible(const char *permissions, const char *name)
{
    return strncmp(permissions, "rw-p", 4) == 0 &&
           (name[0] == '\0' || strcmp(name, "[heap]") == 0);
}

static int handle_batch(pid_t pid, void **pages, size_t count, uint64_t seed,
                        unsigned fast_percent, int fast_node, int slow_node,
                        int pass, uint64_t *page_index,
                        struct counters *counters)
{
    int current[BATCH_PAGES], selected_nodes[BATCH_PAGES];
    int selected_status[BATCH_PAGES];
    void *selected_pages[BATCH_PAGES];
    size_t selected = 0;

    if (move_pages_call(pid, count, pages, NULL, current, 0) < 0) {
        perror("move_pages query");
        return -1;
    }
    for (size_t index = 0; index < count; ++index) {
        int target = pass < 0 ? current[index]
                              : target_node((*page_index)++, seed, fast_percent,
                                            fast_node, slow_node);
        if (pass <= 0) {
            counters->considered++;
            if (current[index] < 0)
                continue;
            counters->present++;
            counters->before_fast += current[index] == fast_node;
            counters->before_slow += current[index] == slow_node;
            counters->unexpected += current[index] != fast_node &&
                                    current[index] != slow_node;
            counters->target_fast += target == fast_node;
            counters->target_slow += target == slow_node;
        }
        if (pass < 0)
            continue;
        if (current[index] < 0 || current[index] == target)
            continue;
        if ((pass == 0 && target != slow_node) ||
            (pass == 1 && target != fast_node))
            continue;
        selected_pages[selected] = pages[index];
        selected_nodes[selected++] = target;
    }
    if (selected == 0)
        return 0;
    long result = move_pages_call(pid, selected, selected_pages, selected_nodes,
                                  selected_status, MPOL_MF_MOVE);
    if (result < 0 && errno != ENOENT) {
        perror("move_pages migrate");
        return -1;
    }
    for (size_t index = 0; index < selected; ++index) {
        if (selected_status[index] >= 0)
            counters->moved++;
        else
            counters->failed++;
    }
    return 0;
}

static int walk(pid_t pid, uint64_t seed, unsigned fast_percent, int fast_node,
                int slow_node, int pass, struct counters *counters)
{
    char path[64], line[4096];
    long page_size = sysconf(_SC_PAGESIZE);
    void *pages[BATCH_PAGES];
    uint64_t page_index = 0;
    snprintf(path, sizeof(path), "/proc/%ld/maps", (long)pid);
    FILE *maps = fopen(path, "r");
    if (maps == NULL) {
        perror(path);
        return -1;
    }
    while (fgets(line, sizeof(line), maps) != NULL) {
        unsigned long start, end, offset, inode;
        char permissions[8], device[32], name[2048] = "";
        int fields = sscanf(line, "%lx-%lx %7s %lx %31s %lu %2047[^\n]",
                            &start, &end, permissions, &offset, device, &inode,
                            name);
        if (fields < 6)
            continue;
        char *trimmed = fields == 7 ? name : name + strlen(name);
        while (*trimmed == ' ' || *trimmed == '\t')
            trimmed++;
        if (!eligible(permissions, trimmed))
            continue;
        size_t count = 0;
        for (unsigned long address = start; address < end; address += page_size) {
            pages[count++] = (void *)address;
            if (count == BATCH_PAGES) {
                if (handle_batch(pid, pages, count, seed, fast_percent,
                                 fast_node, slow_node, pass, &page_index,
                                 counters) != 0)
                    goto fail;
                count = 0;
            }
        }
        if (count != 0 && handle_batch(pid, pages, count, seed, fast_percent,
                                       fast_node, slow_node, pass,
                                       &page_index, counters) != 0)
            goto fail;
    }
    fclose(maps);
    return 0;
fail:
    fclose(maps);
    return -1;
}

static int verify(pid_t pid, int fast_node, int slow_node,
                  struct counters *counters)
{
    char path[64], line[4096];
    long page_size = sysconf(_SC_PAGESIZE);
    void *pages[BATCH_PAGES];
    int status[BATCH_PAGES];
    snprintf(path, sizeof(path), "/proc/%ld/maps", (long)pid);
    FILE *maps = fopen(path, "r");
    if (maps == NULL)
        return -1;
    while (fgets(line, sizeof(line), maps) != NULL) {
        unsigned long start, end, offset, inode;
        char permissions[8], device[32], name[2048] = "";
        int fields = sscanf(line, "%lx-%lx %7s %lx %31s %lu %2047[^\n]",
                            &start, &end, permissions, &offset, device, &inode,
                            name);
        if (fields < 6)
            continue;
        char *trimmed = fields == 7 ? name : name + strlen(name);
        while (*trimmed == ' ' || *trimmed == '\t')
            trimmed++;
        if (!eligible(permissions, trimmed))
            continue;
        size_t count = 0;
        for (unsigned long address = start; address < end; address += page_size) {
            pages[count++] = (void *)address;
            if (count != BATCH_PAGES && address + page_size < end)
                continue;
            if (move_pages_call(pid, count, pages, NULL, status, 0) < 0) {
                fclose(maps);
                return -1;
            }
            for (size_t index = 0; index < count; ++index) {
                counters->after_fast += status[index] == fast_node;
                counters->after_slow += status[index] == slow_node;
            }
            count = 0;
        }
    }
    fclose(maps);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 6) {
        fprintf(stderr, "usage: %s PID SEED FAST_PERCENT|observe FAST_NODE SLOW_NODE\n",
                argv[0]);
        return 2;
    }
    pid_t pid = (pid_t)strtol(argv[1], NULL, 10);
    uint64_t seed = strtoull(argv[2], NULL, 0);
    bool observe = strcmp(argv[3], "observe") == 0;
    unsigned fast_percent = observe ? 0 : (unsigned)strtoul(argv[3], NULL, 10);
    int fast_node = (int)strtol(argv[4], NULL, 10);
    int slow_node = (int)strtol(argv[5], NULL, 10);
    if (pid <= 0 || fast_percent > 100 || fast_node == slow_node)
        return 2;

    struct timespec before, after;
    struct counters counters = {0};
    clock_gettime(CLOCK_MONOTONIC, &before);
    if (observe) {
        if (walk(pid, seed, fast_percent, fast_node, slow_node, -1,
                 &counters) != 0)
            return 1;
        counters.after_fast = counters.before_fast;
        counters.after_slow = counters.before_slow;
    } else if (walk(pid, seed, fast_percent, fast_node, slow_node, 0,
                    &counters) != 0 ||
               walk(pid, seed, fast_percent, fast_node, slow_node, 1,
                    &counters) != 0 ||
               verify(pid, fast_node, slow_node, &counters) != 0) {
        return 1;
    }
    clock_gettime(CLOCK_MONOTONIC, &after);
    double seconds = after.tv_sec - before.tv_sec +
                     (after.tv_nsec - before.tv_nsec) / 1e9;
    printf("{\"pid\":%ld,\"seed\":%" PRIu64
           ",\"mode\":\"%s\",\"fast_percent\":%u,\"fast_node\":%d,\"slow_node\":%d"
           ",\"considered\":%" PRIu64 ",\"present\":%" PRIu64
           ",\"before_fast\":%" PRIu64 ",\"before_slow\":%" PRIu64
           ",\"target_fast\":%" PRIu64 ",\"target_slow\":%" PRIu64
           ",\"after_fast\":%" PRIu64 ",\"after_slow\":%" PRIu64
           ",\"moved\":%" PRIu64 ",\"failed\":%" PRIu64
           ",\"unexpected\":%" PRIu64 ",\"seconds\":%.6f}\n",
           (long)pid, seed, observe ? "observe" : "random", fast_percent,
           fast_node, slow_node,
           counters.considered, counters.present, counters.before_fast,
           counters.before_slow, counters.target_fast, counters.target_slow,
           counters.after_fast, counters.after_slow, counters.moved,
           counters.failed, counters.unexpected, seconds);
    return counters.failed == 0 ? 0 : 1;
}
