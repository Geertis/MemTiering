// SPDX-License-Identifier: GPL-2.0
/* Recyclable order-0 anonymous-allocation reservations for a Sandbox subtree.
 * Credits return only after a tagged physical page is freed, or an in-flight
 * reservation fails. Residency feedback supplies a conservative admission
 * ceiling; it never creates credits. File cache, explicit VMA policy and
 * non-order-0 allocations remain outside this soft placement target.
 * Local Guest page reuse is not evidence of released Host/VMM backing.
 */
#include <linux/cgroup.h>
#include <linux/cpuset.h>
#include <linux/PhaseTieing_alloc.h>
#include <linux/export.h>
#include <linux/kref.h>
#include <linux/mempolicy.h>
#include <linux/mm.h>
#include <linux/rculist.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/jump_label.h>
#include <linux/page_ext.h>
#include "internal.h"

struct demeter_alloc_rule {
	struct list_head link;
	struct kref refs;
	struct cgroup *group;
	spinlock_t lock;
	int fast_node, slow_node;
	unsigned long quota, remaining, resident_bound, live, inflight, live_peak;
	bool active, recycle;
	u64 fast, slow, failed, fallback, native_fallback;
	u64 committed, released, recycled, refunded, tracking_failed, feedback_commits;
};
static LIST_HEAD(alloc_rules);
static DEFINE_MUTEX(alloc_rules_lock);
static DEFINE_STATIC_KEY_FALSE(demeter_alloc_tracking);

#ifdef CONFIG_PAGE_EXTENSION
/* Eight bytes/page (32 MiB for 16 GiB raw RAM), shared by every mode of
 * the matched experiment. The page owns a kref until free or tag transfer. */
struct demeter_alloc_tag { struct demeter_alloc_rule *owner; };
static bool __init need_demeter_alloc_ext(void) { return true; }
struct page_ext_operations demeter_alloc_page_ext_ops = {
	.size = sizeof(struct demeter_alloc_tag), .need = need_demeter_alloc_ext,
};
static struct demeter_alloc_tag *alloc_tag(struct page_ext *ext)
{
	return page_ext_data(ext, &demeter_alloc_page_ext_ops);
}
#endif

/* Called with the rule lock. In-flight pages are charged even across a phase
 * or quota change. A stale resident bound blocks rather than refills. */
static unsigned long alloc_ceiling(struct demeter_alloc_rule *r)
{
	unsigned long occupied = max(r->live, r->resident_bound);
	if (!r->active || occupied >= r->quota ||
	    r->inflight >= r->quota - occupied ||
	    r->quota - occupied - r->inflight <= 128)
		return 0;
	return r->quota - occupied - r->inflight - 128;
}

static void rule_release(struct kref *refs)
{
	struct demeter_alloc_rule *r = container_of(refs, struct demeter_alloc_rule, refs);
	cgroup_put(r->group);
	kfree(r);
}

struct demeter_alloc_rule *demeter_alloc_register(struct cgroup *group, int fast, int slow,
		bool recycle)
{
	struct demeter_alloc_rule *r = kzalloc(sizeof(*r), GFP_KERNEL);
	if (!r) return NULL;
	kref_init(&r->refs);
	spin_lock_init(&r->lock);
	cgroup_get(group);
	r->group = group;
	r->fast_node = fast;
	r->slow_node = slow;
	r->recycle = recycle;
	mutex_lock(&alloc_rules_lock);
	static_branch_enable(&demeter_alloc_tracking);
	list_add_tail_rcu(&r->link, &alloc_rules);
	mutex_unlock(&alloc_rules_lock);
	return r;
}
EXPORT_SYMBOL_NS_GPL(demeter_alloc_register, DEMETER);

void demeter_alloc_unregister(struct demeter_alloc_rule *r)
{
	if (!r) return;
	mutex_lock(&alloc_rules_lock);
	list_del_rcu(&r->link);
	mutex_unlock(&alloc_rules_lock);
	synchronize_rcu();
	kref_put(&r->refs, rule_release);
}
EXPORT_SYMBOL_NS_GPL(demeter_alloc_unregister, DEMETER);

void demeter_alloc_update(struct demeter_alloc_rule *r, unsigned long quota,
		unsigned long resident, bool active, bool new_phase)
{
	unsigned long flags, spare;
	if (!r) return;
	/* Small memcg changes can lag in rstat; retain 512 KiB headroom. This
	 * is a placement target, not a hard cap on all memcg/Guest allocations. */
	spare = quota > resident + 128 ? quota - resident - 128 : 0;
	spin_lock_irqsave(&r->lock, flags);
	if (r->recycle) {
		unsigned long old_quota = r->quota;
		r->quota = quota;
		r->active = active;
		r->resident_bound = max(r->resident_bound, resident);
		if (!active) r->remaining = 0;
		else if (new_phase) r->remaining = alloc_ceiling(r);
		else if (quota > old_quota)
			r->remaining = min(quota, r->remaining + quota - old_quota);
		else r->remaining -= min(r->remaining, old_quota - quota);
		/* Periodic feedback may unblock refunded credit; never grant it. */
	} else {
		/* Same-kernel finite-credit ablation preserves the old allowance. */
		if (!active) r->remaining = 0;
		else if (new_phase) r->remaining = spare;
		else if (quota > r->quota)
			r->remaining = min(spare, r->remaining + quota - r->quota);
		else r->remaining = min(spare, r->remaining - min(r->remaining, r->quota - quota));
	}
	r->quota = quota;
	r->active = active;
	spin_unlock_irqrestore(&r->lock, flags);
}
EXPORT_SYMBOL_NS_GPL(demeter_alloc_update, DEMETER);

void demeter_alloc_feedback(struct demeter_alloc_rule *r, unsigned long resident,
		u64 before_snapshot)
{
	unsigned long flags;
	u64 delta;
	if (!r) return;
	spin_lock_irqsave(&r->lock, flags);
	/* Read the commit watermark before reading memcg. Include all fast
	 * commits during/after that read. Frees during the read are not
	 * subtracted: doing so could count a release twice. */
	if (before_snapshot == U64_MAX) before_snapshot = r->feedback_commits;
	delta = r->committed - min(r->committed, before_snapshot);
	r->resident_bound = max(r->live, resident + min_t(u64, delta, ULONG_MAX - resident));
	r->feedback_commits = r->committed;
	spin_unlock_irqrestore(&r->lock, flags);
}
EXPORT_SYMBOL_NS_GPL(demeter_alloc_feedback, DEMETER);

bool demeter_alloc_take(struct demeter_alloc_rule *r)
{
	unsigned long flags;
	bool take;
	if (!r) return true; /* allocation ablation keeps existing promotion */
	spin_lock_irqsave(&r->lock, flags);
	take = r->active && r->remaining && (!r->recycle || alloc_ceiling(r));
	if (take) { r->remaining--; r->inflight++; }
	spin_unlock_irqrestore(&r->lock, flags);
	return take;
}
EXPORT_SYMBOL_NS_GPL(demeter_alloc_take, DEMETER);

void demeter_alloc_finish(struct demeter_alloc_rule *r, struct page *page, bool promoted)
{
	unsigned long flags;
	bool fast, tagged = false;
	if (!r) return;
	fast = page ? page_to_nid(page) == r->fast_node : promoted;
#ifdef CONFIG_PAGE_EXTENSION
	if (fast && page) {
		struct page_ext *ext = page_ext_get(page);
		if (ext) {
			kref_get(&r->refs);
			tagged = cmpxchg(&alloc_tag(ext)->owner, NULL, r) == NULL;
			if (!tagged) kref_put(&r->refs, rule_release);
			page_ext_put(ext);
		}
	}
#endif
	spin_lock_irqsave(&r->lock, flags);
	WARN_ON_ONCE(!r->inflight);
	r->inflight -= min(r->inflight, 1UL);
	if (fast) {
		r->committed++;
		r->resident_bound++;
		if (tagged) {
			r->live++;
			r->live_peak = max(r->live_peak, r->live);
		} else r->tracking_failed++;
	} else if (r->recycle && r->active) {
		/* The reservation is globally in-flight, not attached to an epoch.
		 * A new phase excluded it too; completion may safely refund it. */
		r->remaining = min(r->quota, r->remaining + 1);
		r->refunded++;
	}
	spin_unlock_irqrestore(&r->lock, flags);
}
EXPORT_SYMBOL_NS_GPL(demeter_alloc_finish, DEMETER);

void demeter_alloc_free(struct page *page, unsigned int order)
{
#ifdef CONFIG_PAGE_EXTENSION
	unsigned int i;
	if (!static_branch_unlikely(&demeter_alloc_tracking)) return;
	for (i = 0; i < (1U << order); i++) {
		struct page_ext *ext = page_ext_get(page + i);
		struct demeter_alloc_rule *r;
		unsigned long flags;
		if (!ext) continue;
		r = xchg(&alloc_tag(ext)->owner, NULL);
		page_ext_put(ext);
		if (!r) continue;
		spin_lock_irqsave(&r->lock, flags);
		WARN_ON_ONCE(!r->live);
		r->live -= min(r->live, 1UL);
		r->released++;
		if (r->recycle && r->active) {
			r->remaining = min(r->quota, r->remaining + 1);
			r->recycled++;
		}
		/* Do not lower the bound here: memcg may already have excluded
		 * this page. A subsequent feedback snapshot unblocks the credit. */
		spin_unlock_irqrestore(&r->lock, flags);
		kref_put(&r->refs, rule_release);
	}
#endif
}

void demeter_alloc_migrate(struct page *old, struct page *new)
{
#ifdef CONFIG_PAGE_EXTENSION
	struct page_ext *src, *dst;
	struct demeter_alloc_rule *r;
	if (!static_branch_unlikely(&demeter_alloc_tracking)) return;
	/* A move to a different tier leaves ownership on the source until it
	 * really reaches free_pages_prepare. A same-tier copy transfers the
	 * tag, so freeing the obsolete source cannot fabricate a credit. */
	if (page_to_nid(old) != page_to_nid(new)) return;
	src = page_ext_get(old); dst = page_ext_get(new);
	if (src && dst) {
		r = xchg(&alloc_tag(src)->owner, NULL);
		if (r) {
			if (WARN_ON_ONCE(cmpxchg(&alloc_tag(dst)->owner, NULL, r) != NULL))
				WRITE_ONCE(alloc_tag(src)->owner, r);
		}
	}
	if (src) page_ext_put(src);
	if (dst) page_ext_put(dst);
#endif
}

void demeter_alloc_stats(struct demeter_alloc_rule *r, struct demeter_alloc_stats *s)
{
	unsigned long flags;
	memset(s, 0, sizeof(*s));
	if (!r) return;
	spin_lock_irqsave(&r->lock, flags);
	s->fast = r->fast; s->slow = r->slow;
	s->failed = r->failed; s->fallback = r->fallback;
	s->native_fallback = r->native_fallback;
	s->remaining = r->recycle ? min(r->remaining, alloc_ceiling(r)) : r->remaining;
	s->balance = r->remaining; s->live = r->live; s->inflight = r->inflight;
	s->live_peak = r->live_peak; s->ceiling = alloc_ceiling(r);
	s->committed = r->committed; s->released = r->released;
	s->recycled = r->recycled; s->refunded = r->refunded;
	s->tracking_failed = r->tracking_failed; s->recycle = r->recycle;
	spin_unlock_irqrestore(&r->lock, flags);
}
EXPORT_SYMBOL_NS_GPL(demeter_alloc_stats, DEMETER);

bool demeter_alloc_vma(gfp_t gfp, unsigned int order, struct vm_area_struct *vma,
		struct page **page)
{
	struct demeter_alloc_rule *r, *found = NULL;
	struct mempolicy *pol = current->mempolicy;
	nodemask_t mask;
	unsigned long flags;
	bool fast;
	gfp_t tier_gfp;
	int nid;
	if (order || (vma->vm_flags & VM_SHARED) ||
	    vma->vm_mm != current->mm || vma->vm_policy ||
	    (pol && pol->mode != MPOL_DEFAULT && pol->mode != MPOL_PREFERRED))
		return false;
	rcu_read_lock();
	list_for_each_entry_rcu(r, &alloc_rules, link)
		if (cgroup_is_descendant(task_dfl_cgroup(current), r->group)) {
			kref_get(&r->refs); found = r; break;
		}
	rcu_read_unlock();
	if (!found) return false;
	r = found;
	mask = cpuset_current_mems_allowed;
	if (!node_isset(r->fast_node, mask) || !node_isset(r->slow_node, mask)) {
		kref_put(&r->refs, rule_release);
		return false;
	}
	fast = demeter_alloc_take(r);
	nid = fast ? r->fast_node : r->slow_node;
	/* A soft tier preference must not reclaim/OOM a restricted node for
	 * every fault while another allowed node can satisfy the request.
	 * Preserve the original reclaim flags for the native-policy fallback.
	 */
	tier_gfp = (gfp | __GFP_NORETRY | __GFP_NOWARN) & ~__GFP_RECLAIM;
	nodes_clear(mask); node_set(nid, mask);
	*page = __alloc_pages_noprof(tier_gfp, 0, nid, &mask);
	if (!*page && fast) {
		nid = r->slow_node;
		nodes_clear(mask); node_set(nid, mask);
		*page = __alloc_pages_noprof(tier_gfp, 0, nid, &mask);
	}
	if (fast) demeter_alloc_finish(r, *page, false);
	spin_lock_irqsave(&r->lock, flags);
	if (!*page) {
		r->failed++;
		r->native_fallback++;
	}
	else if (page_to_nid(*page) == r->fast_node) r->fast++;
	else { r->slow++; if (fast) r->fallback++; }
	/* Lifetime ownership survives unregister and process exit until free. */
	spin_unlock_irqrestore(&r->lock, flags);
	kref_put(&r->refs, rule_release);
	/* A tier target is a soft placement quota, not a nodemask hard cap.
	 * Returning a handled NULL here can cause fault/OOM retries forever:
	 * the selected node is exhausted while the normal policy still has
	 * available memory. Let vma_alloc_folio apply its original policy and
	 * cpuset constraints. This can exceed the DRAM target; residency must
	 * remain charged until the allocator/controller observes release.
	 * native_fallback counts handoffs, not successful native allocations.
	 */
	return *page != NULL;
}
