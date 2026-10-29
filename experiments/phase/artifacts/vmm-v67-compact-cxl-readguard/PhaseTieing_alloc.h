/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_DEMETER_ALLOC_H
#define _LINUX_DEMETER_ALLOC_H
#include <linux/types.h>
struct cgroup;
struct page;
struct vm_area_struct;
struct demeter_alloc_rule;
struct demeter_alloc_stats {
	u64 fast, slow, failed, fallback, native_fallback;
	u64 committed, released, recycled, refunded, tracking_failed;
	unsigned long remaining, balance, live, inflight, live_peak, ceiling;
	bool recycle;
};
struct demeter_alloc_rule *demeter_alloc_register(struct cgroup *, int, int, bool);
void demeter_alloc_unregister(struct demeter_alloc_rule *);
void demeter_alloc_update(struct demeter_alloc_rule *, unsigned long,
		unsigned long, bool, bool);
bool demeter_alloc_take(struct demeter_alloc_rule *);
void demeter_alloc_finish(struct demeter_alloc_rule *, struct page *, bool);
void demeter_alloc_feedback(struct demeter_alloc_rule *, unsigned long, u64);
void demeter_alloc_stats(struct demeter_alloc_rule *, struct demeter_alloc_stats *);
bool demeter_alloc_vma(gfp_t, unsigned int, struct vm_area_struct *, struct page **);
#ifdef CONFIG_NUMA
void demeter_alloc_free(struct page *, unsigned int);
void demeter_alloc_migrate(struct page *, struct page *);
#ifdef CONFIG_PAGE_EXTENSION
struct page_ext_operations;
extern struct page_ext_operations demeter_alloc_page_ext_ops;
#endif
#else
static inline void demeter_alloc_free(struct page *p, unsigned int o) {}
static inline void demeter_alloc_migrate(struct page *old, struct page *new) {}
#endif
#endif
