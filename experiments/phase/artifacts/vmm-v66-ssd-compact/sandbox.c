// SPDX-License-Identifier: GPL-2.0
/* Sandbox-scoped placement. No virtual address, heat or pin crosses an mm.
 * One bounded worker serves all Sandboxes. Perf callbacks only enqueue samples;
 * descriptors are resolved and revalidated immediately before migration.
 */
#include <linux/cgroup.h>
#include <linux/PhaseTieing_alloc.h>
#include <linux/kthread.h>
#include <linux/irq_work.h>
#include <linux/tracepoint.h>
#include <linux/binfmts.h>
#include <linux/mm.h>
#include <linux/mm_inline.h>
#include <linux/mmu_notifier.h>
#include <linux/memcontrol.h>
#include <linux/migrate.h>
#include <linux/min_heap.h>
#include <linux/module.h>
#include <linux/nsproxy.h>
#include <linux/mutex.h>
#include <linux/perf_event.h>
#include <linux/sched/signal.h>
#include <linux/sched/cputime.h>
#include <linux/sort.h>
#include <linux/xarray.h>
#include <linux/vmalloc.h>
#include <../internal.h>
#include "module.h"
#include "mpsc.h"
#include "sandbox.h"

static bool sandbox_allocation_enabled = true;
module_param(sandbox_allocation_enabled, bool, 0444);
static bool sandbox_recycle_credit = true;
module_param(sandbox_recycle_credit, bool, 0444);
/* Allocation-only policy need not create PEBS events or scan address spaces. */
static bool sandbox_sampling_enabled = true;
module_param(sandbox_sampling_enabled, bool, 0444);
/* Physical tier order need not match the Guest's boot memory-node order. */
static int sandbox_fast_node = -1;
module_param(sandbox_fast_node, int, 0444);
static int sandbox_slow_node = -1;
module_param(sandbox_slow_node, int, 0444);
/* Matched heat-only ablation keeps quotas, sampling and placement budgets. */
static bool sandbox_future_benefit = true;
module_param(sandbox_future_benefit, bool, 0444);
static bool sandbox_sample_wakeup = true;
module_param(sandbox_sample_wakeup, bool, 0444);
static unsigned int sandbox_sample_wake_us = 2000;
module_param(sandbox_sample_wake_us, uint, 0444);
static bool sandbox_lifecycle_wakeup = true;
module_param(sandbox_lifecycle_wakeup, bool, 0444);
static bool sandbox_sample_batch = true;
module_param(sandbox_sample_batch, bool, 0444);

#define SB_MAX 8
#define SB_TASK_MAX 64
#define SB_REGION_MAX 8192
#define SB_POOL_MAX 256
#define SB_FILE_POOL_MAX 1024
#define SB_BACKOFF_MAX 8192
#define SB_REGION_SHIFT 16
#define SB_REGION_PAGES (1U << (SB_REGION_SHIFT - PAGE_SHIFT))
#define SB_RING_BYTES (1 << 20)
#define SB_PROFILE_MAX 64
#define SB_NS_MS 1000000ULL
#define SB_RATE_SCALE 1024ULL

static unsigned int sandbox_period_ms = 50;
module_param(sandbox_period_ms, uint, 0444);
static unsigned int sandbox_scan_pages = 8192;
module_param(sandbox_scan_pages, uint, 0444);
static unsigned int sandbox_batch_pages = 256;
module_param(sandbox_batch_pages, uint, 0444);
static unsigned int sandbox_wait_grace_ms = 300;
module_param(sandbox_wait_grace_ms, uint, 0644);
static unsigned int sandbox_cooldown_ms = 1000;
module_param(sandbox_cooldown_ms, uint, 0644);
/* Explicit calibration inputs, in nanoseconds; neither is a PEBS weight. */
static unsigned int sandbox_latency_delta_ns = 80;
module_param(sandbox_latency_delta_ns, uint, 0644);
/* ALL_LOADS and ALL_STORES have comparable retired-instruction units.
 * The legacy latency event remains selectable for a controlled ablation.
 */
static unsigned int sandbox_load_event = 0x81d0;
module_param(sandbox_load_event, uint, 0444);
static bool sandbox_page_heat = true;
module_param(sandbox_page_heat, bool, 0444);
static unsigned int sandbox_confidence_prior = 4;
module_param(sandbox_confidence_prior, uint, 0444);
static unsigned long sandbox_load_sample_period = 65535;
module_param(sandbox_load_sample_period, ulong, 0444);
static unsigned int sandbox_load_weight_permille = 1000;
module_param(sandbox_load_weight_permille, uint, 0444);
static unsigned int sandbox_store_weight_permille = 1000;
module_param(sandbox_store_weight_permille, uint, 0444);
static bool sandbox_sample_diagnostics;
module_param(sandbox_sample_diagnostics, bool, 0444);
static unsigned int sandbox_wait_batch_pages = 1024;
module_param(sandbox_wait_batch_pages, uint, 0444);
/* Bound WAIT reclamation without truncating a normal 256-page batch. */
static bool sandbox_active_kind_learning = true;
module_param(sandbox_active_kind_learning, bool, 0444);
static bool sandbox_skip_unprofitable_scan = true;
module_param(sandbox_skip_unprofitable_scan, bool, 0444);
static unsigned int sandbox_reclaim_time_ms = 20;
module_param(sandbox_reclaim_time_ms, uint, 0444);

static unsigned int sb_heat_shift(void)
{
	return sandbox_page_heat ? PAGE_SHIFT : SB_REGION_SHIFT;
}
static unsigned int sandbox_criticality_permille = 250;
module_param(sandbox_criticality_permille, uint, 0644);
/* Bounded one-second access window, with a half-second startup prior.
 * This avoids extrapolating a single PEBS overflow over a 50 ms tick.
 */
static bool sandbox_smoothed_heat = true;
module_param(sandbox_smoothed_heat, bool, 0644);
static unsigned long sandbox_initial_copy_ns = 20000;
module_param(sandbox_initial_copy_ns, ulong, 0644);

/* Controlled ablation: candidate generation and quotas stay identical. */
static bool sandbox_placement_enabled = true;
module_param(sandbox_placement_enabled, bool, 0644);
static bool sandbox_cross_mm = true;
module_param(sandbox_cross_mm, bool, 0444);
static bool sandbox_file_demotion = true;
module_param(sandbox_file_demotion, bool, 0444);
/* Diagnostics are opt-in and bounded. */
static bool sandbox_diagnostics;
module_param(sandbox_diagnostics, bool, 0444);
#define SB_DIAG_MAX 4096
#define SB_DIAG_FIELDS 24
#define SB_RESOLVE_ROLES 4
#define SB_RESOLVE_REASONS 8
struct sb_diag_record {
 u64 time_ns, serial, decision, phase_seq, generation, peer_generation;
 unsigned int phase, kind, event, n;
 char comm[TASK_COMM_LEN], peer_comm[TASK_COMM_LEN];
 u64 values[SB_DIAG_FIELDS];
};
enum { SB_D_CREATE=1, SB_D_DROP, SB_D_CANDIDATES, SB_D_DECISION,
       SB_D_MOVE, SB_D_EXCHANGE, SB_D_CLEAR, SB_D_RELATIVE, SB_D_FILE,
       SB_D_SAMPLE };
enum { SB_R_SAMPLE, SB_R_HOT, SB_R_COLD, SB_R_VALIDATE };
enum { SB_R_VMA, SB_R_ABSENT, SB_R_FILE, SB_R_LARGE, SB_R_SHARED,
       SB_R_KSM, SB_R_FOREIGN, SB_R_OK };

struct sb_region {
	unsigned long base, last_addr, last_pfn, pfn_addr;
	/* Page mode keeps fractional rates in units of 1/1024 access per ms. */
	u64 accesses, rate, history[4], last_sample_ns, moved_ns, validated_bucket;
	u64 phase_seq;
	u64 heat[8], heat_stamp[8];
	u32 hits[8];
	unsigned int kind;
};
struct sb_mm {
	struct list_head link;
	struct mm_struct *mm;
	struct sandbox *sandbox;
	struct mmu_notifier notifier;
	atomic64_t mapping_epoch;
	u64 epoch_seen, generation, born_ns, diag_samples;
	struct xarray regions;
	struct xarray backoff;
	unsigned int nr_backoff;
	unsigned int nr_regions, refs, kind;
	bool kind_valid;
	unsigned long cursor, hot_cursor;
	u64 diag_info[14];
	u64 heat[8], heat_stamp[8], heat_hits[8], sample_phase_seq;
	u64 mean_rate, mean_history[4], window_hits;
	unsigned long anonymous_pages;
	char comm[TASK_COMM_LEN];
};
struct sb_task {
	struct list_head link;
	struct task_struct *task;
	struct sb_mm *space;
	struct perf_event *events[MAX_EVENTS];
	struct sandbox *sandbox;
	bool seen, enabled;
};
struct sb_sample {
	u64 generation, epoch, phase_seq, time_ns, addr;
	u32 accesses, source;
	/* Only diagnostics carry this tail; the normal queue record is 48 B. */
	u64 period, latency, data_src;
};
static size_t sb_sample_bytes(void)
{
	return sandbox_sample_diagnostics ? sizeof(struct sb_sample) :
		offsetof(struct sb_sample, period);
}
static bool sb_sample_valid(const struct sb_sample *sample, ssize_t received)
{
	return received == (ssize_t)sb_sample_bytes() && sample->source < MAX_EVENTS;
}
struct sb_candidate {
	struct sb_mm *space;
	unsigned long addr, pfn;
	u64 epoch, benefit_ns;
	unsigned int evidence;
};
struct sb_lifetime {
	char comm[TASK_COMM_LEN];
	unsigned int kind, count;
	u64 duration_ns;
};
struct sandbox {
	struct kobject kobj;
	unsigned int id;
	struct cgroup *cgroup;
	struct demeter_alloc_rule *alloc_rule;
	struct list_head tasks, spaces;
	mpsc_t samples;
	struct irq_work sample_wake;
	atomic64_t sample_wakeups;
	atomic64_t sample_last_wake_ns;
	unsigned int nr_tasks, nr_regions;
	u64 sequence, phase_seq, phase_start_ns, deadline_ns;
	unsigned int phase, kind;
	unsigned long quota_pages, fast_pages, slow_pages;
	u64 feedback_ns, decision_ns, copy_ns, exchange_cost_ns, placement_ns;
	struct sb_lifetime lifetimes[SB_PROFILE_MAX];
	struct sb_candidate hot[SB_POOL_MAX], cold[SB_POOL_MAX];
	u64 sampled, stale, overflow, registrations, perf_errors, capacity_misses;
	u64 registration_lifecycle_skips, registration_partial_unwinds;
	u64 decisions, hot_candidates, cold_candidates, rejected_benefit;
	u64 promotions, demotions, exchanges, cross_mm_exchanges, failed, invalid;
	u64 worker_ns, worker_cpu_ns, migration_ns;
	u64 old_gate_profitable_pairs, old_gate_cross_pairs, relative_exchanges;
	atomic64_t captured_load, captured_store, rejected_tid;
	u64 file_demotions, file_failed, file_scanned, file_eligible, file_skipped;
	u64 load_weighted, store_weighted, reclaim_timeouts, unprofitable_scan_skips, lifetime_kind_assignments;
	unsigned int nr_backoff;
	u64 backoff_limited;
	u64 sample_format_errors, samples_received, sample_cache_hits, sample_drain_ns, sample_budget_hits, sample_max_age_ns;
	unsigned int sample_cpu, sample_burst;
	bool sample_backlog;
	u64 maintenance_ns, maintenance_phase_seq;
	struct folio *file_pool[SB_FILE_POOL_MAX];
	struct sb_mm *mm_order[SB_TASK_MAX];
	bool cold_used[SB_POOL_MAX];
	atomic64_t sampling_ns;
 struct sb_diag_record *diag;
 unsigned int diag_head, diag_tail;
 u64 diag_serial, diag_dropped;
 u64 resolve[SB_RESOLVE_ROLES][SB_RESOLVE_REASONS];
};
static DEFINE_MUTEX(sb_lock);
static struct kobject *sb_root;
static struct sandbox *sandboxes[SB_MAX];
static unsigned int nr_sandboxes;
static unsigned long sb_budget_pages;
static struct task_struct *sb_worker;
static DEFINE_SPINLOCK(sb_wake_lock);
static struct irq_work sb_lifecycle_work;
static atomic_t sb_lifecycle_pending = ATOMIC_INIT(0);
static atomic_t sb_consumer_pending = ATOMIC_INIT(0);
static atomic64_t sb_fork_notifications = ATOMIC64_INIT(0);
static atomic64_t sb_exec_notifications = ATOMIC64_INIT(0);
static atomic64_t sb_lifecycle_discoveries = ATOMIC64_INIT(0);
static struct tracepoint *sb_fork_tp, *sb_exec_tp;
static bool sb_fork_registered, sb_exec_registered;
static atomic64_t mm_sequence = ATOMIC64_INIT(0);
extern struct mutex demeter_sysfs_lock;
extern int demeter_migrate_folio_to_node(struct folio *, int);
extern int folio_exchange_isolated(struct folio *, struct folio *, enum migrate_mode);
extern bool folio_isolate_lru(struct folio *);
extern void folio_putback_lru(struct folio *);
static struct folio *sb_resolve(struct sb_mm *, unsigned long, unsigned int);

static void sb_diag_emit(struct sandbox *sb, struct sb_mm *space,
 struct sb_mm *peer, unsigned int event, const u64 *values, unsigned int n)
{
 struct sb_diag_record *r;
 if (!sb->diag)
  return;
 if (sb->diag_head - sb->diag_tail == SB_DIAG_MAX) {
  sb->diag_dropped++;
  sb->diag_tail++;
 }
 r = &sb->diag[sb->diag_head++ % SB_DIAG_MAX];
 memset(r, 0, sizeof(*r));
 r->time_ns = ktime_get_ns(); r->serial = ++sb->diag_serial;
 r->decision = sb->decisions; r->phase_seq = sb->phase_seq;
 r->phase = sb->phase; r->kind = sb->kind; r->event = event;
 if (space) { r->generation = space->generation; memcpy(r->comm, space->comm, TASK_COMM_LEN); }
 if (peer) { r->peer_generation = peer->generation; memcpy(r->peer_comm, peer->comm, TASK_COMM_LEN); }
 r->n = min(n, (unsigned int)SB_DIAG_FIELDS);
 memcpy(r->values, values, r->n * sizeof(u64));
}
#define SB_DIAG(sb, space, peer, event, ...) do { \
 if (sandbox_diagnostics) { \
  const u64 _args[] = { __VA_ARGS__ }; \
  sb_diag_emit(sb, space, peer, event, _args, ARRAY_SIZE(_args)); \
 } \
} while (0)

bool sandbox_enabled(void)
{
	return READ_ONCE(nr_sandboxes) != 0;
}

static int sb_invalidate(struct mmu_notifier *notifier,
			 const struct mmu_notifier_range *range)
{
	struct sb_mm *space = container_of(notifier, struct sb_mm, notifier);
	/* Migrations change PFNs, not virtual identities. Unmap/mremap and VMA
	 * replacement invalidate the entire bounded context conservatively.
	 */
	if (range->event == MMU_NOTIFY_UNMAP ||
	    range->event == MMU_NOTIFY_PROTECTION_VMA)
		atomic64_inc(&space->mapping_epoch);
	return 0;
}
static const struct mmu_notifier_ops sb_notifier_ops = {
	.invalidate_range_start = sb_invalidate,
};

static void sb_clear_regions(struct sandbox *sb, struct sb_mm *space)
{
	unsigned long index;
 struct sb_region *region;
 if (space->nr_regions)
  SB_DIAG(sb, space, NULL, SB_D_CLEAR, space->nr_regions,
          space->epoch_seen, atomic64_read(&space->mapping_epoch));
	xa_for_each(&space->regions, index, region)
		kfree(region);
	xa_destroy(&space->regions);
	xa_init(&space->regions);
	xa_destroy(&space->backoff);
	xa_init(&space->backoff);
	sb->nr_backoff -= space->nr_backoff; space->nr_backoff = 0;
	memset(space->heat, 0, sizeof(space->heat));
	memset(space->heat_stamp, 0, sizeof(space->heat_stamp));
	memset(space->heat_hits, 0, sizeof(space->heat_hits));
	memset(space->mean_history, 0, sizeof(space->mean_history));
	space->mean_rate = space->window_hits = 0;
	sb->nr_regions -= space->nr_regions;
	space->nr_regions = 0;
	space->epoch_seen = atomic64_read(&space->mapping_epoch);
}

static struct sb_lifetime *sb_profile(struct sandbox *sb, struct sb_mm *space,
				     unsigned int kind, bool create)
{
	struct sb_lifetime *empty = NULL;
	unsigned int i;
	for (i = 0; i < SB_PROFILE_MAX; i++) {
		struct sb_lifetime *p = &sb->lifetimes[i];
		if (!p->count) {
			if (!empty)
				empty = p;
		} else if (p->kind == kind && !strncmp(p->comm, space->comm, TASK_COMM_LEN))
			return p;
	}
	if (create && empty) {
		strscpy(empty->comm, space->comm, TASK_COMM_LEN);
		empty->kind = kind;
		return empty;
	}
	return NULL;
}

static void sb_space_drop(struct sandbox *sb, struct sb_mm *space)
{
	struct sb_lifetime *p = space->kind_valid ? sb_profile(sb, space, space->kind, true) : NULL;
 SB_DIAG(sb, space, NULL, SB_D_DROP, ktime_get_ns() - space->born_ns,
         space->nr_regions, space->refs, space->diag_samples, space->kind, space->kind_valid);
	/* Only complete mm lifetimes are observations. Controller teardown must
	 * not train the duration predictor with censored observations.
	 */
	if (p && !space->refs) {
		u64 duration = ktime_get_ns() - space->born_ns;
		p->duration_ns = p->count ? (3 * p->duration_ns + duration) / 4 : duration;
		p->count = min(p->count + 1, 1000U);
	}
	list_del(&space->link);
	mmu_notifier_unregister(&space->notifier, space->mm);
	sb_clear_regions(sb, space);
	mmput(space->mm);
	kfree(space);
}

/* perf callbacks can run in NMI. Defer scheduler interaction to irq_work.
 * The lock synchronizes the worker pointer with module exit; rule/queue
 * teardown waits for pending work after releasing all producer events. */
static void sb_sample_wake(struct irq_work *work)
{
 struct sandbox *sb = container_of(work, struct sandbox, sample_wake);
 unsigned long flags;
 spin_lock_irqsave(&sb_wake_lock, flags);
 if (sb_worker) {
  atomic64_inc(&sb->sample_wakeups);
  wake_up_process(sb_worker);
 }
 spin_unlock_irqrestore(&sb_wake_lock, flags);
}

/* A scheduler tracepoint may hold locks: only set a flag and defer the wake.
 * No Sandbox pointer is dereferenced here. The worker performs cgroup-scoped
 * discovery, event creation and mm revalidation in normal thread context.
 * Notifications are Guest-wide while at least one Sandbox is registered. */
static void sb_lifecycle_wake(struct irq_work *work)
{
 unsigned long flags;
 spin_lock_irqsave(&sb_wake_lock, flags);
 if (sb_worker) wake_up_process(sb_worker);
 spin_unlock_irqrestore(&sb_wake_lock, flags);
}
static void sb_note_fork(void *unused, struct task_struct *parent,
                         struct task_struct *child)
{
 if (!READ_ONCE(nr_sandboxes)) return;
 atomic64_inc(&sb_fork_notifications);
 atomic_set(&sb_lifecycle_pending, 1);
 irq_work_queue(&sb_lifecycle_work);
}
static void sb_note_exec(void *unused, struct task_struct *task, pid_t old_pid,
                         struct linux_binprm *bprm)
{
 if (!READ_ONCE(nr_sandboxes)) return;
 atomic64_inc(&sb_exec_notifications);
 atomic_set(&sb_lifecycle_pending, 1);
 irq_work_queue(&sb_lifecycle_work);
}
static void sb_find_lifecycle_tp(struct tracepoint *tp, void *unused)
{
 if (!strcmp(tp->name, "sched_process_fork")) sb_fork_tp = tp;
 if (!strcmp(tp->name, "sched_process_exec")) sb_exec_tp = tp;
}
static void sb_lifecycle_unregister(void)
{
 if (sb_exec_registered) tracepoint_probe_unregister(sb_exec_tp, sb_note_exec, NULL);
 if (sb_fork_registered) tracepoint_probe_unregister(sb_fork_tp, sb_note_fork, NULL);
 if (sb_exec_registered || sb_fork_registered) tracepoint_synchronize_unregister();
 sb_exec_registered = sb_fork_registered = false;
 irq_work_sync(&sb_lifecycle_work);
}

static void sb_overflow(struct perf_event *event, struct perf_sample_data *data,
			struct pt_regs *regs)
{
	struct sb_task *target = event->overflow_handler_context;
	struct sb_sample sample;
	u64 started = ktime_get_mono_fast_ns(), period, capture_index;
	perf_prepare_sample(data, event, regs);
	capture_index = atomic64_inc_return(event->attr.config == sandbox_load_event ?
		&target->sandbox->captured_load : &target->sandbox->captured_store);
	/* exec may change this task's mm before the discovery tick. An old mm
	 * can remain live in a CLONE_VM sibling: generation lookup alone would
	 * then accept a new address in the old context. Reject it at capture.
	 */
	if (data->tid_entry.tid != target->task->pid ||
	    READ_ONCE(target->task->mm) != target->space->mm) {
		atomic64_inc(&target->sandbox->rejected_tid);
		return;
	}
	sample.generation = target->space->generation;
	sample.epoch = atomic64_read(&target->space->mapping_epoch);
	sample.phase_seq = READ_ONCE(target->sandbox->phase_seq);
	sample.time_ns = ktime_get_mono_fast_ns();
	sample.addr = data->addr;
	sample.source = event->attr.config == sandbox_load_event ? EVENT_LOAD : EVENT_STORE;
	period = event->attr.sample_period;
	if (sandbox_sample_diagnostics) {
		sample.period = period;
		sample.latency = data->weight.full;
		sample.data_src = data->data_src.val;
	}
	sample.accesses = min(div64_u64(min(period, 1000000000ULL) *
		(sample.source == EVENT_LOAD ? sandbox_load_weight_permille :
		 sandbox_store_weight_permille), 1000), 1000000000ULL);
	/* Non-inheriting events are explicitly installed on each discovered
	 * thread, so each mm has a single shared heat table without duplicates.
	 */
	if (mpsc_send(target->sandbox->samples, &sample, sb_sample_bytes()) > 0 &&
	    sandbox_sample_batch && sandbox_sample_wakeup) {
		u64 previous = atomic64_read(&target->sandbox->sample_last_wake_ns);
		if (!(capture_index % 128) || (sandbox_sample_wake_us &&
		    started >= previous && started - previous >= (u64)sandbox_sample_wake_us * 1000)) {
			atomic64_cmpxchg(&target->sandbox->sample_last_wake_ns, previous, started);
			atomic_set(&sb_consumer_pending, 1);
			irq_work_queue(&target->sandbox->sample_wake);
		}
	}
	atomic64_add(ktime_get_mono_fast_ns() - started, &target->sandbox->sampling_ns);
}

static void sb_task_drop(struct sandbox *sb, struct sb_task *target)
{
	unsigned int i;
	for (i = 0; i < MAX_EVENTS; i++)
		if (target->events[i])
			perf_event_release_kernel(target->events[i]);
	list_del(&target->link);
	target->space->refs--;
	put_task_struct(target->task);
	kfree(target);
	sb->nr_tasks--;
}

/* Birth in WAIT gives no execution-kind information. Assign the first
 * observed ACTIVE kind, once; later WAIT or tool transitions must not rewrite
 * the completed-process lifetime class. This also covers processes discovered
 * before the runtime's start event, without sharing their page addresses. */
static void sb_note_kind(struct sb_mm *space, unsigned int phase, unsigned int kind)
{
	if (sandbox_active_kind_learning && phase && !space->kind_valid) {
		space->kind = kind;
		space->kind_valid = true;
		space->sandbox->lifetime_kind_assignments++;
	}
}

static struct sb_mm *sb_space_get(struct sandbox *sb, struct mm_struct *mm,
				  struct task_struct *task)
{
	struct sb_mm *space;
	list_for_each_entry(space, &sb->spaces, link)
		if (space->mm == mm) {
			mmput(mm);
			return space;
		}
	space = kzalloc(sizeof(*space), GFP_KERNEL);
	if (!space) {
		mmput(mm);
		return NULL;
	}
	space->mm = mm;
	space->sandbox = sb;
	space->generation = atomic64_inc_return(&mm_sequence);
	space->born_ns = ktime_get_ns();
	space->kind = sb->kind;
	space->kind_valid = !sandbox_active_kind_learning;
	sb_note_kind(space, sb->phase, sb->kind);
	get_task_comm(space->comm, task);
	xa_init(&space->regions);
	xa_init(&space->backoff);
	atomic64_set(&space->mapping_epoch, 0);
	space->notifier.ops = &sb_notifier_ops;
	if (mmu_notifier_register(&space->notifier, mm)) {
		mmput(mm);
		kfree(space);
		return NULL;
	}
	list_add_tail(&space->link, &sb->spaces);
 SB_DIAG(sb, space, NULL, SB_D_CREATE, task->pid, space->kind);
 return space;
}

/* A task reference pins task_struct, not its execution or address space.
 * perf registration can sleep: validate on both sides without holding the
 * task lock across perf_event_create_kernel_counter(). */
static bool sb_registration_live(struct sb_task *target)
{
	bool live;
	task_lock(target->task);
	live = pid_alive(target->task) &&
		!(target->task->flags & PF_EXITING) &&
		target->task->mm == target->space->mm;
	task_unlock(target->task);
	return live;
}

static noinline void sb_discover(struct sandbox *sb)
{
	struct task_struct *leader, *task, *found[SB_TASK_MAX];
	struct sb_task *target, *tmp;
	struct sb_mm *space, *next;
	unsigned int count = 0, i;
	list_for_each_entry(target, &sb->tasks, link)
		target->seen = false;
	rcu_read_lock();
	for_each_process_thread(leader, task) {
		if (task->flags & (PF_KTHREAD | PF_EXITING))
			continue;
		if (!cgroup_is_descendant(task_dfl_cgroup(task), sb->cgroup))
			continue;
		if (count == SB_TASK_MAX) {
			sb->capacity_misses++;
			break;
		}
		get_task_struct(task);
		found[count++] = task;
	}
	rcu_read_unlock();
	for (i = 0; i < count; i++) {
		struct mm_struct *mm = get_task_mm(found[i]);
		bool exists = false;
		if (!mm) {
			put_task_struct(found[i]);
			continue;
		}
		list_for_each_entry(target, &sb->tasks, link) {
			if (target->task == found[i] && target->space->mm == mm) {
				target->seen = true;
				exists = true;
				break;
			}
		}
		if (exists) {
			mmput(mm);
			put_task_struct(found[i]);
			continue;
		}
		space = sb_space_get(sb, mm, found[i]);
		target = kzalloc(sizeof(*target), GFP_KERNEL);
		if (!space || !target) {
			kfree(target);
			put_task_struct(found[i]);
			continue;
		}
		target->task = found[i];
		target->space = space;
		target->sandbox = sb;
		target->seen = true;
		space->refs++;
		list_add_tail(&target->link, &sb->tasks);
		sb->nr_tasks++;
		for (unsigned int e = 0; sandbox_sampling_enabled && e < MAX_EVENTS; e++) {
			struct perf_event_attr attr = event_attrs[e];
			struct perf_event *event;
			if (!sb_registration_live(target))
				goto lifecycle_skip;
			if (e == EVENT_LOAD) {
				attr.config = sandbox_load_event;
				attr.sample_period = sandbox_load_sample_period;
				if (sandbox_load_event == 0x81d0)
					attr.config1 = 0;
			}
			if (sandbox_sample_diagnostics)
				attr.sample_type |= PERF_SAMPLE_DATA_SRC;
			attr.inherit = 0;
			attr.inherit_thread = 0;
			attr.disabled = 1;
			event = perf_event_create_kernel_counter(&attr, -1, target->task,
							 sb_overflow, target);
			if (IS_ERR(event)) {
				/* Only a confirmed exit/exec is a benign lifecycle race.
				 * ESRCH for a still-live matching task remains an error. */
				if (PTR_ERR(event) == -ESRCH && !sb_registration_live(target))
					goto lifecycle_skip;
				sb->perf_errors++;
				pr_warn_ratelimited("demeter_sandbox: PEBS pid=%d event=%u error=%ld\n",
						target->task->pid, e, PTR_ERR(event));
				continue;
			}
			target->events[e] = event;
			if (!sb_registration_live(target))
				goto lifecycle_skip;
			if (sb->phase) {
				perf_event_enable(event);
				target->enabled = true;
			}
		}
		sb->registrations++;
		continue;
lifecycle_skip:
		sb->registration_lifecycle_skips++;
		for (unsigned int e = 0; e < MAX_EVENTS; e++)
			if (target->events[e]) {
				sb->registration_partial_unwinds++;
				break;
			}
		sb_task_drop(sb, target);
	}
	list_for_each_entry_safe(target, tmp, &sb->tasks, link)
		if (!target->seen)
			sb_task_drop(sb, target);
	list_for_each_entry_safe(space, next, &sb->spaces, link)
		if (!space->refs)
			sb_space_drop(sb, space);
}

/* Stay on one CPU for at most 64 records, then rotate, including empty CPUs.
 * Unlike mpsc_recv, this does not start every search at CPU zero. */
static ssize_t sb_recv(struct sandbox *sb, struct sb_sample *sample)
{
 unsigned int tried;
 ssize_t n;
 if (!sandbox_sample_batch) return mpsc_recv(sb->samples, sample, sizeof(*sample));
 for (tried = 0; tried < num_online_cpus(); tried++) {
  if (!cpu_online(sb->sample_cpu)) sb->sample_cpu = cpumask_first(cpu_online_mask);
  n = mpsc_recv_cpu(sb->samples, sb->sample_cpu, sample, sizeof(*sample));
  if (n > 0 && ++sb->sample_burst < 64) return n;
  sb->sample_burst = 0;
  sb->sample_cpu = cpumask_next(sb->sample_cpu, cpu_online_mask);
  if (sb->sample_cpu >= nr_cpu_ids) sb->sample_cpu = cpumask_first(cpu_online_mask);
  if (n > 0) return n;
 }
 return -EAGAIN;
}

static noinline void sb_samples(struct sandbox *sb)
{
	struct sb_sample sample;
	unsigned int batch = 0;
	ssize_t received;
	u64 started = ktime_get_ns();
	sb->sample_backlog = false;
	while (batch++ < 32768 &&
	       !sb->sample_backlog && (received = sb_recv(sb, &sample)) > 0) {
		struct sb_mm *space, *match = NULL;
		struct sb_region *r;
		unsigned long index;
		if (!sb_sample_valid(&sample, received)) {
			sb->sample_format_errors++;
			continue;
		}
		sb->samples_received++;
		if (sandbox_sample_batch && !(batch % 64) &&
		    ktime_get_ns() - started > 5 * SB_NS_MS) {
			/* Process this consumed record; yield before consuming another. */
			sb->sample_backlog = true;
		}
		sb->sample_max_age_ns = max(sb->sample_max_age_ns,
			ktime_get_ns() - min(ktime_get_ns(), sample.time_ns));
		index = sample.addr >> sb_heat_shift();
		list_for_each_entry(space, &sb->spaces, link)
			if (space->generation == sample.generation) {
				match = space;
				break;
			}
		if (!match || !sample.addr || !sb->phase || sample.phase_seq != sb->phase_seq ||
		    sample.epoch != atomic64_read(&match->mapping_epoch)) {
			sb->stale++;
			continue;
		}
		if (match->epoch_seen != sample.epoch)
			sb_clear_regions(sb, match);
		if (sample.source == EVENT_LOAD) sb->load_weighted += sample.accesses;
		else sb->store_weighted += sample.accesses;
		if (sandbox_sample_diagnostics)
			SB_DIAG(sb, match, NULL, SB_D_SAMPLE, sample.source,
				sample.addr, sample.period, sample.accesses, sample.latency,
				sb_heat_shift(), sample.data_src, sample.time_ns);
		r = xa_load(&match->regions, index);
		if (sandbox_page_heat) {
			struct folio *f;
			u64 bucket = div64_u64(sample.time_ns, 125 * SB_NS_MS);
			unsigned int slot = bucket % ARRAY_SIZE(match->heat);
			if (!sample.accesses) continue;
			if (!sandbox_sample_batch || !r || r->validated_bucket != bucket + 1) {
				mmap_read_lock(match->mm);
				f = sb_resolve(match, sample.addr, SB_R_SAMPLE);
				mmap_read_unlock(match->mm);
				if (!f) continue;
				folio_put(f);
			} else sb->sample_cache_hits++;
			if (match->sample_phase_seq != sb->phase_seq) {
				memset(match->heat, 0, sizeof(match->heat));
				memset(match->heat_stamp, 0, sizeof(match->heat_stamp));
				memset(match->heat_hits, 0, sizeof(match->heat_hits));
				match->sample_phase_seq = sb->phase_seq;
			}
			if (match->heat_stamp[slot] != bucket) {
				match->heat_stamp[slot] = bucket;
				match->heat[slot] = match->heat_hits[slot] = 0;
			}
			match->heat[slot] = min(match->heat[slot] + sample.accesses, 1000000000ULL);
			match->heat_hits[slot]++;
		}
		r = xa_load(&match->regions, index);
		if (!r) {
			if (sb->nr_regions == SB_REGION_MAX) {
				sb->overflow++;
				continue;
			}
			r = kzalloc(sizeof(*r), GFP_KERNEL);
			if (!r) {
				sb->overflow++;
				continue;
			}
			r->base = index << sb_heat_shift();
			if (xa_err(xa_store(&match->regions, index, r, GFP_KERNEL))) {
				kfree(r);
				sb->overflow++;
				continue;
			}
			match->nr_regions++;
			sb->nr_regions++;
		}
		r->validated_bucket = div64_u64(sample.time_ns, 125 * SB_NS_MS) + 1;
		/* The history belongs to this exact mm and mapping epoch. A phase
		 * change never imports the preceding process's virtual addresses.
		 */
		if (r->phase_seq != sb->phase_seq) {
			r->accesses = 0;
			r->rate = 0;
			memset(r->heat, 0, sizeof(r->heat));
			memset(r->heat_stamp, 0, sizeof(r->heat_stamp));
			memset(r->hits, 0, sizeof(r->hits));
			r->phase_seq = sb->phase_seq;
		}
		{
			u64 bucket = div64_u64(sample.time_ns, 125 * SB_NS_MS);
			unsigned int slot = bucket % ARRAY_SIZE(r->heat);
			if (r->heat_stamp[slot] != bucket) {
				r->heat_stamp[slot] = bucket;
				r->heat[slot] = 0;
				r->hits[slot] = 0;
			}
			r->heat[slot] = min(r->heat[slot] + sample.accesses, 1000000000ULL);
			r->hits[slot] = min(r->hits[slot] + 1, 1000000U);
		}
		r->accesses = min(r->accesses + sample.accesses, 1000000000ULL);
		r->last_addr = sample.addr & PAGE_MASK;
		r->last_sample_ns = sample.time_ns;
		r->kind = sb->kind;
		sb->sampled++;
  if (sandbox_diagnostics) match->diag_samples++;
	}
	if (batch >= 32768) sb->sample_backlog = true;
	sb->sample_budget_hits += sb->sample_backlog;
	sb->sample_drain_ns += ktime_get_ns() - started;
}

/* Caller holds mmap_read_lock. FOLL_GET references never escape candidate
 * generation. Only private, writable, order-0 anonymous folios are supported.
 */
static struct folio *sb_resolve(struct sb_mm *space, unsigned long addr, unsigned int role)
{
 struct vm_area_struct *vma = vma_lookup(space->mm, addr);
 struct page *page;
 struct folio *folio = NULL;
 struct mem_cgroup *memcg;
 unsigned int reason;
 bool owned;
 if (!vma || !(vma->vm_flags & VM_WRITE) || (vma->vm_flags & (VM_SHARED | VM_LOCKED))) {
  reason = SB_R_VMA; goto reject;
 }
 page = follow_page(vma, addr, FOLL_GET | FOLL_DUMP);
 if (IS_ERR_OR_NULL(page)) { reason = SB_R_ABSENT; goto reject; }
 folio = page_folio(page);
 if (!folio_test_anon(folio)) { reason = SB_R_FILE; goto reject; }
 if (folio_order(folio)) { reason = SB_R_LARGE; goto reject; }
 if (folio_mapcount(folio) != 1) { reason = SB_R_SHARED; goto reject; }
 if (folio_test_ksm(folio)) { reason = SB_R_KSM; goto reject; }
 rcu_read_lock();
 memcg = folio_memcg(folio);
 owned = memcg && cgroup_is_descendant(memcg->css.cgroup, space->sandbox->cgroup);
 rcu_read_unlock();
 if (!owned) { reason = SB_R_FOREIGN; goto reject; }
 if (sandbox_diagnostics) space->sandbox->resolve[role][SB_R_OK]++;
 return folio;
reject:
 if (sandbox_diagnostics) space->sandbox->resolve[role][reason]++;
 if (folio) folio_put(folio);
 return NULL;
}

static bool sb_on_cooldown(struct sb_mm *space, unsigned long addr, u64 now)
{
	void *entry = xa_load(&space->backoff, addr >> PAGE_SHIFT);
	return entry && div64_u64(now, SB_NS_MS) - (xa_to_value(entry) - 1) < sandbox_cooldown_ms;
}

static void sb_prune_backoff(struct sb_mm *space, u64 now)
{
	unsigned long index; void *entry;
	xa_for_each(&space->backoff, index, entry)
		if (div64_u64(now, SB_NS_MS) - (xa_to_value(entry) - 1) >= sandbox_cooldown_ms) {
			xa_erase(&space->backoff, index);
			space->nr_backoff--; space->sandbox->nr_backoff--;
		}
}

static bool sb_begin_move(struct sb_candidate *c, u64 now)
{
	struct sb_mm *space = c->space;
	unsigned long index = c->addr >> PAGE_SHIFT;
	bool existing = xa_load(&space->backoff, index) != NULL;
	if ((!existing && space->sandbox->nr_backoff == SB_BACKOFF_MAX) ||
	    xa_err(xa_store(&space->backoff, index,
		 xa_mk_value(div64_u64(now, SB_NS_MS) + 1), GFP_NOWAIT))) {
		space->sandbox->backoff_limited++; return false;
	}
	if (!existing) { space->nr_backoff++; space->sandbox->nr_backoff++; }
	return true;
}

static u64 sb_benefit(struct sandbox *sb, struct sb_mm *space,
		      struct sb_region *r, u64 now)
{
	u64 horizon, accesses, benefit, rate;
	struct sb_lifetime *p;
	if (!sb->phase || (r && now - r->moved_ns < sandbox_cooldown_ms * SB_NS_MS))
		return 0;
	horizon = sandbox_future_benefit ?
		(sb->deadline_ns > now ? sb->deadline_ns - now : 100 * SB_NS_MS) :
		500 * SB_NS_MS;
	/* Unknown processes receive a conservative half-second lifetime prior.
	 * Completed lifetimes share only duration experience, never page heat.
	 */
	p = sb_profile(sb, space, sb->kind, false);
	if (sandbox_future_benefit && p && p->count >= 2) {
		u64 age = now - space->born_ns;
		/* Surviving the old mean is evidence, not an imminent-exit signal. */
		horizon = min(horizon, p->duration_ns > age ? p->duration_ns - age : 500 * SB_NS_MS);
	} else
		horizon = min(horizon, 500 * SB_NS_MS);
	horizon = min(horizon, 5000 * SB_NS_MS);
	if (!r && !sandbox_page_heat) return 0;
	rate = r ? r->rate : space->mean_rate;
	if (r && (r->phase_seq != sb->phase_seq || now - r->last_sample_ns > (sandbox_smoothed_heat ? 1000 : 200) * SB_NS_MS))
		rate = sandbox_future_benefit ? r->history[sb->kind] / 4 : 0;
	/* Keep fractional page rates until the final ns conversion. */
	accesses = mul_u64_u64_div_u64(rate, horizon, SB_NS_MS);
	if (!sandbox_page_heat) accesses /= SB_REGION_PAGES;
	accesses = min(accesses, 1000000000ULL * (sandbox_page_heat ? SB_RATE_SCALE : 1));
	benefit = div64_u64(accesses * min(sandbox_latency_delta_ns, 5000U) *
		min(sandbox_criticality_permille, 1000U),
		1000 * (sandbox_page_heat ? SB_RATE_SCALE : 1));
	return benefit;
}

static u64 sb_copy_gate(struct sandbox *sb)
{
	return sandbox_future_benefit ? sb->copy_ns : 0;
}

static u64 sb_exchange_gate(struct sandbox *sb)
{
	return sandbox_future_benefit ? sb->exchange_cost_ns : 0;
}

static void sb_space_rate(struct sandbox *sb, struct sb_mm *space, u64 now)
{
	u64 total = 0, hits = 0, bucket = div64_u64(now, 125 * SB_NS_MS);
	u64 observation = clamp_t(u64, now - sb->phase_start_ns, 500 * SB_NS_MS, 1000 * SB_NS_MS);
	space->anonymous_pages = max(get_mm_counter(space->mm, MM_ANONPAGES), 1UL);
	if (space->sample_phase_seq != sb->phase_seq) {
		space->mean_rate = sandbox_future_benefit ?
			space->mean_history[sb->kind] / 4 : 0;
		space->window_hits = 0;
		return;
	}
	for (unsigned int j = 0; j < ARRAY_SIZE(space->heat); j++)
		if (bucket >= space->heat_stamp[j] && bucket - space->heat_stamp[j] < 8) {
			total += space->heat[j]; hits += space->heat_hits[j];
		}
	space->window_hits = hits;
	space->mean_rate = div64_u64(div64_u64(total * SB_NS_MS * SB_RATE_SCALE, observation), space->anonymous_pages);
	if (space->mean_rate)
		space->mean_history[sb->kind] = (3 * space->mean_history[sb->kind] + space->mean_rate) / 4;
}

static u64 sb_page_rate(struct sb_mm *space, u64 total, u64 hits, u64 observation)
{
	u64 raw = div64_u64(total * SB_NS_MS * SB_RATE_SCALE, observation);
	u64 prior, denom;
	if (!sandbox_confidence_prior) return raw;
	/* One hit in a sparsely covered address space is weak evidence of a
	 * hotspot. Pool toward this exact mm's observed mean, never another mm.
	 * A frequent page can still overcome the prior. No absolute-cold gate. */
	prior = sandbox_confidence_prior * max_t(u64, 1,
		div64_u64(space->anonymous_pages, max(space->window_hits, 1ULL)));
	prior = min(prior, 1000000000ULL);
	denom = hits + prior;
	if (raw >= space->mean_rate)
		return space->mean_rate + mul_u64_u64_div_u64(raw - space->mean_rate, hits, denom);
	return raw + mul_u64_u64_div_u64(space->mean_rate - raw, prior, denom);
}

static int sb_hot_cmp(const void *a, const void *b)
{
	const struct sb_candidate *x = a, *y = b;
	return (x->benefit_ns < y->benefit_ns) - (x->benefit_ns > y->benefit_ns);
}
static int sb_cold_cmp(const void *a, const void *b)
{
	const struct sb_candidate *x = a, *y = b;
	return (x->benefit_ns > y->benefit_ns) - (x->benefit_ns < y->benefit_ns);
}

/* Keep the best K of all pages examined in this bounded scan, rather than
 * the first K that satisfy an absolute temperature threshold. The root is
 * the least useful retained entry, so replacement costs O(log K).
 */
static void sb_candidate_swap(void *a, void *b)
{
	struct sb_candidate tmp = *(struct sb_candidate *)a;
	*(struct sb_candidate *)a = *(struct sb_candidate *)b;
	*(struct sb_candidate *)b = tmp;
}
static bool sb_hot_less(const void *a, const void *b)
{
	return ((const struct sb_candidate *)a)->benefit_ns <
	       ((const struct sb_candidate *)b)->benefit_ns;
}
static bool sb_cold_less(const void *a, const void *b)
{
	return sb_hot_less(b, a);
}
static const struct min_heap_callbacks sb_hot_heap = {
	.elem_size = sizeof(struct sb_candidate), .less = sb_hot_less,
	.swp = sb_candidate_swap,
};
static const struct min_heap_callbacks sb_cold_heap = {
	.elem_size = sizeof(struct sb_candidate), .less = sb_cold_less,
	.swp = sb_candidate_swap,
};
static void sb_offer(struct min_heap *heap, struct sb_candidate *c,
		     const struct min_heap_callbacks *ops)
{
	if (heap->nr < heap->size)
		min_heap_push(heap, c, ops);
	else if (ops->less(heap->data, c))
		min_heap_pop_push(heap, c, ops);
}

static void sb_candidates(struct sandbox *sb, u64 now, u64 elapsed,
			  unsigned int *hot_count, unsigned int *cold_count)
{
	struct sb_mm *space, **order = sb->mm_order;
	struct min_heap hot = { .data = sb->hot, .size = SB_POOL_MAX };
	struct min_heap cold = { .data = sb->cold, .size = SB_POOL_MAX };
	unsigned int spaces = 0, per_mm;
	bool background_hot = false;
	bool wait_settled = !sb->phase &&
		now - sb->phase_start_ns >= sandbox_wait_grace_ms * SB_NS_MS;

	list_for_each_entry(space, &sb->spaces, link)
		if (spaces < SB_TASK_MAX)
			order[spaces++] = space;
	per_mm = max(1U, sandbox_scan_pages / max(spaces, 1U));
	/* Rotate traversal to avoid permanently privileging the first mm when
	 * benefits tie. Heat/addresses remain strictly scoped to their mm.
	 */
	for (unsigned int i = 0; i < spaces; i++) {
		struct sb_region *r;
		unsigned long index;
		unsigned int hot_left = per_mm;
		u64 *info;
		space = order[(i + sb->decisions) % spaces];
		info = space->diag_info;
		memset(info, 0, sizeof(space->diag_info));
		if (space->epoch_seen != atomic64_read(&space->mapping_epoch))
			sb_clear_regions(sb, space);
		sb_prune_backoff(space, now);
		if (sandbox_page_heat) {
			sb_space_rate(sb, space, now);
			background_hot |= sb_benefit(sb, space, NULL, now) >
				min(sb_copy_gate(sb), sb_exchange_gate(sb));
		}
		mmap_read_lock(space->mm);
		/* Update every tracked region, even when the resolution budget is
		 * exhausted. A cursor then rotates the hot-page resolution window.
		 */
		xa_for_each(&space->regions, index, r) {
			if (sb->phase && r->phase_seq == sb->phase_seq) {
				u64 per_ms = div64_u64(r->accesses * SB_NS_MS,
						     max(elapsed, SB_NS_MS));
				if (sandbox_smoothed_heat) {
					u64 bucket = div64_u64(now, 125 * SB_NS_MS), total = 0, hits = 0;
					u64 observation = clamp_t(u64, now - sb->phase_start_ns,
								500 * SB_NS_MS, 1000 * SB_NS_MS);
					for (unsigned int j = 0; j < ARRAY_SIZE(r->heat); j++)
						if (bucket >= r->heat_stamp[j] && bucket - r->heat_stamp[j] < 8) {
							total += r->heat[j];
							hits += r->hits[j];
						}
					r->rate = div64_u64(total * SB_NS_MS, observation);
					/* Shrink sparse evidence; this is a conservative prior,
					 * not a statistical guarantee or extra hardware samples. */
					if (sandbox_page_heat)
						r->rate = sb_page_rate(space, total, hits, observation);
				} else
					r->rate = (r->rate + per_ms *
					(sandbox_page_heat ? SB_RATE_SCALE : 1)) / 2;
				r->accesses = 0;
				if (r->rate)
					r->history[sb->kind] = (3 * r->history[sb->kind] + r->rate) / 4;
			}
		}
		xa_for_each_start(&space->regions, index, r, space->hot_cursor) {
			struct folio *sampled;
			u64 benefit = sb_benefit(sb, space, r, now);
			space->hot_cursor = index + 1;
			if (sb_on_cooldown(space, r->base, now)) continue;
			if (!benefit) { info[6]++; continue; }
			info[13] = max(info[13], benefit);
			if (benefit <= min(sb_copy_gate(sb), sb_exchange_gate(sb))) { info[7]++; continue; }
			if (!hot_left) { space->hot_cursor = index; break; }
			sampled = sb_resolve(space, r->last_addr, SB_R_SAMPLE);
			hot_left--;
			if (!sampled) { info[8]++; continue; }
			if (r->last_pfn && r->pfn_addr == r->last_addr &&
			    r->last_pfn != folio_pfn(sampled)) {
				r->rate = r->accesses = 0;
				memset(r->heat, 0, sizeof(r->heat));
				memset(r->hits, 0, sizeof(r->hits));
				memset(r->history, 0, sizeof(r->history));
				benefit = 0;
			}
			r->last_pfn = folio_pfn(sampled);
			r->pfn_addr = r->last_addr;
			folio_put(sampled);
			if (!benefit) continue;
			for (unsigned long addr = r->base;
			     addr < r->base + (1UL << sb_heat_shift()) && hot_left;
			     addr += PAGE_SIZE) {
				struct folio *folio = sb_resolve(space, addr, SB_R_HOT);
				hot_left--;
				if (!folio) continue;
				if (folio_nid(folio) == sandbox_slow_node) {
					struct sb_candidate c = {
						.space = space, .addr = addr, .pfn = folio_pfn(folio),
						.epoch = space->epoch_seen, .benefit_ns = benefit,
						.evidence = sandbox_page_heat ? 1 : 2,
					};
					sb_offer(&hot, &c, &sb_hot_heap);
				}
				folio_put(folio);
			}
		}
		if (hot_left) space->hot_cursor = 0;
		mmap_read_unlock(space->mm);
		info[9] = space->nr_regions; info[10] = now - space->born_ns;
		info[11] = space->refs; info[12] = space->diag_samples;
	}
	/* Resolve slow candidates first across ALL address spaces. No cold scan
	 * is needed when no profitable promotion/substitution is possible and
	 * residency is already within quota. Background (unsampled) hot pages
	 * and quota reclamation remain reasons to scan. This changes no gate.
	 */
	if (sandbox_skip_unprofitable_scan && !hot.nr && !background_hot &&
	    sb->fast_pages <= sb->quota_pages) {
		sb->unprofitable_scan_skips++;
		goto candidates_done;
	}
	for (unsigned int i = 0; i < spaces; i++) {
		struct sb_region *r;
		unsigned int cold_left = per_mm;
		u64 *info;
		space = order[(i + sb->decisions) % spaces];
		info = space->diag_info;
		mmap_read_lock(space->mm);
		{
			VMA_ITERATOR(vmi, space->mm, space->cursor);
			struct vm_area_struct *vma;
			bool completed = true;
			for_each_vma(vmi, vma) {
				unsigned long addr = max(space->cursor, vma->vm_start);
				if (!(vma->vm_flags & VM_WRITE) ||
				    (vma->vm_flags & (VM_SHARED | VM_LOCKED))) {
					space->cursor = vma->vm_end;
					continue;
				}
				for (; addr < vma->vm_end; addr += PAGE_SIZE) {
					struct folio *folio;
					u64 benefit;
					if (!cold_left) {
						space->cursor = addr; completed = false;
						goto scan_done;
					}
					cold_left--; info[2]++;
					folio = sb_resolve(space, addr, SB_R_COLD);
					if (!folio) { info[5]++; continue; }
					if (folio_nid(folio) == sandbox_fast_node) info[3]++;
					if (folio_nid(folio) == sandbox_slow_node) info[4]++;
					r = xa_load(&space->regions, addr >> sb_heat_shift());
					benefit = sb_benefit(sb, space, r, now);
					if (sb_on_cooldown(space, addr, now)) { folio_put(folio); continue; }
					if (folio_nid(folio) == sandbox_fast_node &&
					    (!r || now - r->moved_ns >= sandbox_cooldown_ms * SB_NS_MS) &&
					    (wait_settled || sb->phase)) {
						struct sb_candidate c = {
							.space = space, .addr = addr, .pfn = folio_pfn(folio),
							.epoch = space->epoch_seen, .benefit_ns = benefit,
							.evidence = r ? (sandbox_page_heat ? 1 : 2) : (sandbox_page_heat ? 3 : 0),
						};
						sb_offer(&cold, &c, &sb_cold_heap);
					} else if (sandbox_page_heat && !r && folio_nid(folio) == sandbox_slow_node &&
						   benefit > min(sb_copy_gate(sb), sb_exchange_gate(sb))) {
						struct sb_candidate c = {
							.space = space, .addr = addr, .pfn = folio_pfn(folio),
							.epoch = space->epoch_seen, .benefit_ns = benefit,
							.evidence = r ? 1 : 3,
						};
						sb_offer(&hot, &c, &sb_hot_heap);
					}
					folio_put(folio);
				}
				space->cursor = vma->vm_end;
			}
scan_done:
			if (completed) space->cursor = 0;
		}
		mmap_read_unlock(space->mm);
		info[9] = space->nr_regions; info[10] = now - space->born_ns;
		info[11] = space->refs; info[12] = space->diag_samples;
	}
candidates_done:
	*hot_count = hot.nr; *cold_count = cold.nr;
	sort(sb->hot, hot.nr, sizeof(sb->hot[0]), sb_hot_cmp, NULL);
	sort(sb->cold, cold.nr, sizeof(sb->cold[0]), sb_cold_cmp, NULL);
	for (unsigned int i = 0; i < spaces; i++) {
		space = order[i];
		for (unsigned int j = 0; j < hot.nr; j++)
			space->diag_info[0] += sb->hot[j].space == space;
		for (unsigned int j = 0; j < cold.nr; j++)
			space->diag_info[1] += sb->cold[j].space == space;
		if (sandbox_diagnostics)
			sb_diag_emit(sb, space, NULL, SB_D_CANDIDATES, space->diag_info, 14);
	}
	sb->hot_candidates += hot.nr;
	sb->cold_candidates += cold.nr;
}

static struct folio *sb_validate(struct sandbox *sb, struct sb_candidate *c, int nid)
{
	struct folio *folio;
	if (!c->space->refs || c->epoch != atomic64_read(&c->space->mapping_epoch))
		goto invalid;
	mmap_read_lock(c->space->mm);
	folio = sb_resolve(c->space, c->addr, SB_R_VALIDATE);
	mmap_read_unlock(c->space->mm);
	if (!folio)
		goto invalid;
	if (folio_pfn(folio) != c->pfn || folio_nid(folio) != nid) {
		folio_put(folio);
		goto invalid;
	}
	return folio;
invalid:
	sb->invalid++;
	return NULL;
}

static void sb_mark_moved(struct sb_candidate *c, u64 now)
{
	struct sb_region *r = xa_load(&c->space->regions, c->addr >> sb_heat_shift());
	if (r) {
		r->moved_ns = now;
		/* Migration changes the PFN while preserving virtual identity. */
		r->last_pfn = 0;
	}
}

static int sb_move(struct sandbox *sb, struct sb_candidate *c, bool up, u64 now)
{
	u64 started = ktime_get_ns(), core, cost;
	struct folio *folio = NULL;
	int err = -ENOMEM;
	if (!sb_begin_move(c, now)) goto done;
	folio = sb_validate(sb, c, up ? sandbox_slow_node : sandbox_fast_node); err = -ENOENT;
	if (!folio) goto done;
	core = ktime_get_ns();
	/* The helper consumes the FOLL_GET reference, including error paths. */
	err = demeter_migrate_folio_to_node(folio, up ? sandbox_fast_node : sandbox_slow_node);
	sb->migration_ns += ktime_get_ns() - core;
	if (!err) {
		if (up) {
			sb->promotions++;
			sb->fast_pages++;
			sb->slow_pages -= min(sb->slow_pages, 1UL);
		} else {
			sb->demotions++;
			sb->fast_pages -= min(sb->fast_pages, 1UL);
			sb->slow_pages++;
		}
	} else sb->failed++;
	sb_mark_moved(c, now);
done:
	cost = ktime_get_ns() - started;
	sb->placement_ns += cost;
	/* Include validation, allocation and migration; failures consume time too. */
	if (!err) sb->copy_ns = (7 * sb->copy_ns + cost) / 8;
	else sb->copy_ns = max(sb->copy_ns, cost);
	SB_DIAG(sb, c->space, NULL, SB_D_MOVE, up, (u64)(s64)err,
		folio ? 1 : 0, c->benefit_ns, sb->copy_ns, cost, c->addr, c->evidence);
	return err;
}

static bool sb_isolate(struct folio *folio)
{
	if (!folio_isolate_lru(folio)) {
		folio_put(folio);
		return false;
	}
	/* Remove FOLL_GET; retain only the isolation reference, as required by
	 * Demeter's exchange support check. No long-lived candidate pins.
	 */
	folio_put(folio);
	node_stat_mod_folio(folio, NR_ISOLATED_ANON, 1);
	return true;
}
static void sb_putback(struct folio *folio)
{
	node_stat_mod_folio(folio, NR_ISOLATED_ANON, -1);
	folio_putback_lru(folio);
}
static int sb_exchange(struct sandbox *sb, struct sb_candidate *hot,
			struct sb_candidate *cold, u64 now)
{
	u64 started = ktime_get_ns(), core, cost;
	struct folio *p = NULL, *d;
	int err = -ENOMEM, stage = 0;
	if (!sb_begin_move(hot, now) || !sb_begin_move(cold, now)) goto done;
	p = sb_validate(sb, hot, sandbox_slow_node); err = -ENOENT;
	if (!p) goto done;
	stage = 1;
	d = sb_validate(sb, cold, sandbox_fast_node);
	if (!d) { folio_put(p); goto done; }
	err = -EBUSY; stage = 2;
	if (!sb_isolate(p)) { folio_put(d); goto done; }
	stage = 3;
	if (!sb_isolate(d)) { sb_putback(p); goto done; }
	stage = 4;
	core = ktime_get_ns();
	err = folio_exchange_isolated(p, d, MIGRATE_ASYNC);
	sb->migration_ns += ktime_get_ns() - core;
	if (!err) {
		sb->exchanges++;
		sb->cross_mm_exchanges += hot->space != cold->space;
	} else sb->failed++;
	sb_putback(p);
	sb_putback(d);
	sb_mark_moved(hot, now);
	sb_mark_moved(cold, now);
done:
	cost = ktime_get_ns() - started;
	sb->placement_ns += cost;
	/* Exchange is a distinct operation; do not estimate it as two copies. */
	if (!err) sb->exchange_cost_ns = (7 * sb->exchange_cost_ns + cost) / 8;
	else sb->exchange_cost_ns = max(sb->exchange_cost_ns, cost);
	SB_DIAG(sb, hot->space, cold->space, SB_D_EXCHANGE, (u64)(s64)err,
		stage, hot->benefit_ns, cold->benefit_ns, sb->exchange_cost_ns, cost,
		hot->addr, cold->addr, hot->evidence, cold->evidence);
	return err;
}

/* Unmapped, clean, unreferenced order-0 inactive file cache is not reachable
 * from an mm's VMA scan. Walk only the registered memcg subtree. Pins last
 * only for this decision and are consumed on every success/error path.
 * During ACTIVE, active/referenced cache is protected. After WAIT grace,
 * clean active cache can also move. Mapped/dirty/large/shmem are excluded.
 */
static bool sb_file_ok(struct sandbox *sb, struct folio *f)
{
	struct mem_cgroup *memcg;
	bool owned;
	if (!folio_test_lru(f) || folio_nid(f) != sandbox_fast_node ||
	    !folio_is_file_lru(f) || folio_order(f) || folio_mapped(f) ||
	    folio_test_dirty(f) || folio_test_writeback(f) ||
	    folio_test_unevictable(f) ||
	    (sb->phase && (folio_test_active(f) || folio_test_referenced(f))))
		return false;
	rcu_read_lock();
	memcg = folio_memcg(f);
	owned = memcg && cgroup_is_descendant(memcg->css.cgroup, sb->cgroup);
	rcu_read_unlock();
	return owned;
}
static unsigned int sb_demote_files(struct sandbox *sb, unsigned int budget)
{
	struct cgroup_subsys_state *root, *pos = NULL, *held = NULL;
	unsigned int scanned = 0, count = 0, visited = 0, attempts = 0;
	unsigned int last_lru = sb->phase ? LRU_INACTIVE_FILE : LRU_ACTIVE_FILE;
	u64 migration_ns = 0;
	u64 stop_ns = ktime_get_ns() + sandbox_reclaim_time_ms * SB_NS_MS;
	if (!sandbox_file_demotion || !budget || sb->fast_pages <= sb->quota_pages)
		return 0;
	/* Multi-generation pages are on separate lists; this walk covers only
	 * classic inactive file LRU, without changing kernel aging state. */
	root = cgroup_get_e_css(sb->cgroup, &memory_cgrp_subsys);
	if (!root) return 0;
	/* An ancestor's effective memcg must never broaden Sandbox ownership. */
	if (root->cgroup != sb->cgroup) { css_put(root); return 0; }
	while (scanned < sandbox_scan_pages && count < min(budget, SB_FILE_POOL_MAX) &&
	       visited++ < SB_TASK_MAX) {
		struct lruvec *lv;
		struct folio *f;
		unsigned long flags;
		rcu_read_lock();
		pos = css_next_descendant_pre(pos, root);
		while (pos && !css_tryget(pos)) pos = css_next_descendant_pre(pos, root);
		rcu_read_unlock();
		if (held) css_put(held);
		held = pos;
		if (!pos) break;
		lv = mem_cgroup_lruvec(mem_cgroup_from_css(pos), NODE_DATA(sandbox_fast_node));
		spin_lock_irqsave(&lv->lru_lock, flags);
		for (unsigned int lru = LRU_INACTIVE_FILE; lru <= last_lru; lru++) {
			list_for_each_entry_reverse(f, &lv->lists[lru], lru) {
				if (scanned == sandbox_scan_pages || count == min(budget, SB_FILE_POOL_MAX))
					break;
				scanned++;
				if (!sb_file_ok(sb, f) || !folio_try_get(f)) { sb->file_skipped++; continue; }
				sb->file_pool[count++] = f;
			}
		}
		spin_unlock_irqrestore(&lv->lru_lock, flags);
	}
	if (held) css_put(held);
	css_put(root);
	sb->file_scanned += scanned; sb->file_eligible += count;
	for (unsigned int i = 0; i < count; i++) {
		struct folio *f = sb->file_pool[i];
		int err;
		if (ktime_get_ns() >= stop_ns) {
			sb->reclaim_timeouts++;
			for (; i < count; i++) folio_put(sb->file_pool[i]);
			break;
		}
		if (sb->fast_pages <= sb->quota_pages || !sb_file_ok(sb, f)) {
			folio_put(f); continue;
		}
		attempts++;
		u64 started = ktime_get_ns();
		err = demeter_migrate_folio_to_node(f, sandbox_slow_node);
		migration_ns += ktime_get_ns() - started;
		if (!err) {
			sb->file_demotions++; sb->demotions++;
			sb->fast_pages -= min(sb->fast_pages, 1UL); sb->slow_pages++;
		} else { sb->file_failed++; sb->failed++; }
	}
	sb->migration_ns += migration_ns;
	SB_DIAG(sb, NULL, NULL, SB_D_FILE, scanned, count, attempts,
		sb->file_demotions, sb->file_failed, sb->fast_pages);
	return attempts;
}

static void sb_decide(struct sandbox *sb, u64 now)
{
	unsigned int nh, nc, budget = sb->phase ? sandbox_batch_pages :
		 sandbox_wait_batch_pages, reclaim;
	bool *used = sb->cold_used;
	u64 elapsed = sb->decision_ns ? now - sb->decision_ns : sandbox_period_ms * SB_NS_MS;
	u64 info[20] = {sb->fast_pages, sb->quota_pages, 0, 0, sb->copy_ns};
	u64 extra[5] = {0};
	struct sb_mm *space;
	memset(used, 0, sizeof(sb->cold_used));
	sb->decision_ns = now;
	sb->decisions++;
	list_for_each_entry(space, &sb->spaces, link) info[5]++;
	sb_candidates(sb, now, elapsed, &nh, &nc);
	info[2] = nh; info[3] = nc;
	/* Count opportunities, including those excluded by the old cold gate.
	 * These are descriptor pairs, not unique pages or measured speedups.
	 */
	if (sandbox_diagnostics)
	for (unsigned int hi = 0; hi < nh; hi++)
		for (unsigned int ci = 0; ci < nc; ci++) {
			struct sb_candidate *h = &sb->hot[hi], *c = &sb->cold[ci];
			bool cross = h->space != c->space;
			bool profit = h->benefit_ns > c->benefit_ns + sb_exchange_gate(sb);
			if (sandbox_diagnostics && cross) {
				info[16]++; info[17] += profit;
			}
			if (profit && c->benefit_ns >= sb->copy_ns) {
				extra[0]++; extra[1] += cross;
			}
		}
	sb->old_gate_profitable_pairs += extra[0];
	sb->old_gate_cross_pairs += extra[1];
	if (!READ_ONCE(sandbox_placement_enabled)) { extra[4] = 1; goto diag_done; }
	if (now - sb->feedback_ns > 500 * SB_NS_MS) {
		info[14] = 1; goto diag_done;
	}
	/* Reserve half of the copy budget for valuable substitutions when ACTIVE
	 * has hot pages. Reclamation keeps making progress, but cannot consume
	 * the entire tick or all descriptors before a net-zero substitution.
	 * A swap may run during overshoot: it grants no new residency credit.
	 */
	reclaim = sb->phase && nh ? budget / 2 : budget;
	extra[3] = reclaim;
	if (sb->phase || now - sb->phase_start_ns >= sandbox_wait_grace_ms * SB_NS_MS) {
		unsigned int files = sb_demote_files(sb, reclaim);
		info[7] += files; budget -= files; reclaim -= files;
		/* Keep descriptors as well as copy budget for substitution. */
		if (sb->phase && nh) reclaim = min(reclaim, nc / 2);
		for (unsigned int ci = 0; ci < nc && reclaim &&
		     sb->fast_pages > sb->quota_pages; ci++) {
			used[ci] = true; info[7]++;
			sb_move(sb, &sb->cold[ci], false, now);
			reclaim--; budget--;
		}
	}
	if (!sb->phase) goto diag_done;
	for (unsigned int hi = 0; hi < nh && budget; hi++) {
		struct sb_candidate *hot = &sb->hot[hi], *cold = NULL;
		unsigned int ci;
		/* A swap can be cheaper than allocating a destination folio. Keep
		 * that positive-gain alternative even when DRAM has spare quota.
		 */
		if (sb->fast_pages < sb->quota_pages && hot->benefit_ns > sb_copy_gate(sb) &&
		    demeter_alloc_take(sb->alloc_rule)) {
			struct folio *placed = NULL;
			int moved;
			info[6]++; moved = sb_move(sb, hot, true, now); budget--;
			if (!moved) {
				mmap_read_lock(hot->space->mm);
				placed = sb_resolve(hot->space, hot->addr, SB_R_VALIDATE);
				mmap_read_unlock(hot->space->mm);
			}
			/* A successful move with a concurrently disappearing mapping
			 * stays charged conservatively if its destination cannot be tagged. */
			demeter_alloc_finish(sb->alloc_rule, placed ? &placed->page : NULL, !moved);
			if (placed) folio_put(placed);
			continue;
		}
		if (budget < 2) break;
		for (ci = 0; ci < nc; ci++)
			if (!used[ci] && (sandbox_cross_mm || sb->cold[ci].space == hot->space)) {
				cold = &sb->cold[ci]; break;
			}
		if (!cold) { info[12]++; continue; }
		info[15]++;
		if (hot->benefit_ns <= cold->benefit_ns + sb_exchange_gate(sb)) {
			sb->rejected_benefit++; info[11]++;
			/* A rejected pairing never consumes the cold descriptor. */
			continue;
		}
		if (hot->space == cold->space && sandbox_diagnostics)
			for (unsigned int j = 0; j < nc; j++)
				if (!used[j] && hot->space != sb->cold[j].space &&
				    hot->benefit_ns > sb->cold[j].benefit_ns + sb_exchange_gate(sb)) {
					info[10]++; break;
				}
		info[8]++; info[9] += hot->space != cold->space;
		if (cold->benefit_ns >= sb->copy_ns) {
			extra[2]++;
			if (!sb_exchange(sb, hot, cold, now)) sb->relative_exchanges++;
		} else sb_exchange(sb, hot, cold, now);
		used[ci] = true; budget -= 2;
	}
diag_done:
	info[13] = budget == 0; info[18] = sb->fast_pages; info[19] = sb->copy_ns;
	if (sandbox_diagnostics) sb_diag_emit(sb, NULL, NULL, SB_D_DECISION, info, ARRAY_SIZE(info));
	SB_DIAG(sb, NULL, NULL, SB_D_RELATIVE, extra[0], extra[1], extra[2], extra[3], extra[4]);
}

static int sb_work(void *unused)
{
	while (!kthread_should_stop()) {
		unsigned int i;
		bool backlog = false;
		bool lifecycle;
		mutex_lock(&sb_lock);
		atomic_set(&sb_consumer_pending, 0);
		lifecycle = atomic_xchg(&sb_lifecycle_pending, 0);
		if (lifecycle) atomic64_inc(&sb_lifecycle_discoveries);
		for (i = 0; i < nr_sandboxes; i++) {
			struct sandbox *sb = sandboxes[i];
			struct sb_task *target;
			u64 started = ktime_get_ns();
			u64 user_before, system_before, user_after, system_after;
			if (!sb->cgroup)
				continue;
			if (!sandbox_sampling_enabled && !sandbox_placement_enabled)
				continue;
			task_cputime_adjusted(current, &user_before, &system_before);
			if (lifecycle || !sandbox_sample_batch || started - sb->maintenance_ns >= sandbox_period_ms * SB_NS_MS ||
			    sb->maintenance_phase_seq != sb->phase_seq) {
			sb_discover(sb);
			list_for_each_entry(target, &sb->tasks, link) {
				bool enable = sb->phase != 0;
				if (target->enabled == enable)
					continue;
				for (unsigned int e = 0; e < MAX_EVENTS; e++)
					if (target->events[e]) {
						if (enable)
							perf_event_enable(target->events[e]);
						else
							perf_event_disable(target->events[e]);
					}
				target->enabled = enable;
			}
			sb->maintenance_ns = started;
			sb->maintenance_phase_seq = sb->phase_seq;
			}
			sb_samples(sb);
			backlog |= sb->sample_backlog;
			if (!sandbox_sample_batch || !sb->decision_ns ||
			    ktime_get_ns() - sb->decision_ns >= sandbox_period_ms * SB_NS_MS)
				sb_decide(sb, ktime_get_ns());
			sb->worker_ns += ktime_get_ns() - started;
			task_cputime_adjusted(current, &user_after, &system_after);
			sb->worker_cpu_ns += user_after + system_after - user_before - system_before;
		}
		mutex_unlock(&sb_lock);
		/* Set the sleep state before checking persistent notifications. This
		 * closes the running-worker wakeup race for both IRQ sources. */
		set_current_state(TASK_INTERRUPTIBLE);
		if (!atomic_read(&sb_lifecycle_pending) &&
		    !atomic_read(&sb_consumer_pending) && !kthread_should_stop())
			schedule_timeout(backlog && sandbox_sample_batch ? 1 :
				msecs_to_jiffies(sandbox_period_ms));
		__set_current_state(TASK_RUNNING);
	}
	return 0;
}

static void sb_reset(struct sandbox *sb)
{
	struct sb_task *target, *tmp;
	struct sb_mm *space, *next;
	list_for_each_entry_safe(target, tmp, &sb->tasks, link)
		sb_task_drop(sb, target);
	list_for_each_entry_safe(space, next, &sb->spaces, link) {
		space->refs = 1; /* Censored teardown must not train the lifetime prior. */
		sb_space_drop(sb, space);
	}
	irq_work_sync(&sb->sample_wake);
	demeter_alloc_unregister(sb->alloc_rule);
	sb->alloc_rule = NULL;
	if (sb->cgroup) {
		cgroup_put(sb->cgroup);
		sb->cgroup = NULL;
	}
}
static void sb_release(struct kobject *kobj)
{
	struct sandbox *sb = container_of(kobj, struct sandbox, kobj);
	mutex_lock(&sb_lock);
	sb_reset(sb);
	mutex_unlock(&sb_lock);
	if (sb->samples)
		mpsc_drop(sb->samples);
	vfree(sb->diag);
 kfree(sb);
}
#define SB_FROM_KOBJ(k) container_of(k, struct sandbox, kobj)
static ssize_t cgroup_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	struct sandbox *sb = SB_FROM_KOBJ(kobj);
	int ret;
	mutex_lock(&sb_lock);
	buf[0] = '\0';
	ret = sb->cgroup ? cgroup_path_ns(sb->cgroup, buf, PAGE_SIZE - 2,
					current->nsproxy->cgroup_ns) : 0;
	if (ret >= 0)
		ret = strlcat(buf, "\n", PAGE_SIZE);
	mutex_unlock(&sb_lock);
	return ret;
}
static ssize_t cgroup_store(struct kobject *kobj, struct kobj_attribute *attr,
			    const char *buf, size_t count)
{
	struct sandbox *sb = SB_FROM_KOBJ(kobj);
	struct cgroup *group = NULL;
	char *path = kstrndup(buf, count, GFP_KERNEL);
	int err = 0;
	if (!path)
		return -ENOMEM;
	strim(path);
	if (strcmp(path, "-")) {
		if (path[0] != '/' || !strcmp(path, "/")) {
			kfree(path);
			return -EINVAL;
		}
		group = cgroup_get_from_path(path);
		if (IS_ERR(group)) {
			kfree(path);
			return PTR_ERR(group);
		}
	}
	kfree(path);
	mutex_lock(&sb_lock);
	for (unsigned int i = 0; group && i < nr_sandboxes; i++) {
		struct cgroup *other = sandboxes[i]->cgroup;
		if (sandboxes[i] != sb && other &&
		    (cgroup_is_descendant(group, other) || cgroup_is_descendant(other, group))) {
			err = -EINVAL;
			goto out;
		}
	}
	sb_reset(sb);
	if (group && sandbox_allocation_enabled) {
		sb->alloc_rule = demeter_alloc_register(group, sandbox_fast_node, sandbox_slow_node,
			sandbox_recycle_credit);
		if (!sb->alloc_rule) { err = -ENOMEM; goto out; }
	}
	sb->cgroup = group;
	group = NULL;
	sb->phase = 0;
	sb->phase_start_ns = ktime_get_ns();
out:
	mutex_unlock(&sb_lock);
	if (group)
		cgroup_put(group);
	return err ? err : count;
}
static ssize_t phase_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	struct sandbox *sb = SB_FROM_KOBJ(kobj);
	return sysfs_emit(buf, "%u %u\n", READ_ONCE(sb->phase), READ_ONCE(sb->kind));
}
static ssize_t phase_store(struct kobject *kobj, struct kobj_attribute *attr,
			   const char *buf, size_t count)
{
	struct sandbox *sb = SB_FROM_KOBJ(kobj);
	unsigned int phase, kind, horizon_ms;
	if (sscanf(buf, "%u %u %u", &phase, &kind, &horizon_ms) != 3 ||
	    phase > 1 || kind > 3 || horizon_ms > 60000)
		return -EINVAL;
	mutex_lock(&sb_lock);
	sb->phase = phase;
	sb->kind = kind;
	{
		struct sb_mm *space;
		list_for_each_entry(space, &sb->spaces, link)
			sb_note_kind(space, phase, kind);
	}
	demeter_alloc_update(sb->alloc_rule, sb->quota_pages, sb->fast_pages, phase, true);
	sb->phase_seq++;
	sb->phase_start_ns = ktime_get_ns();
	sb->deadline_ns = sb->phase_start_ns + horizon_ms * SB_NS_MS;
	if (sb_worker)
		wake_up_process(sb_worker);
	mutex_unlock(&sb_lock);
	return count;
}
/* Keepalive changes neither phase identity nor the heat/grace clock. */
static ssize_t horizon_ms_store(struct kobject *kobj, struct kobj_attribute *attr,
			       const char *buf, size_t count)
{
	struct sandbox *sb = SB_FROM_KOBJ(kobj);
	unsigned int horizon;
	if (kstrtouint(buf, 10, &horizon) || horizon < 100 || horizon > 5000)
		return -EINVAL;
	mutex_lock(&sb_lock);
	if (sb->phase) sb->deadline_ns = ktime_get_ns() + horizon * SB_NS_MS;
	mutex_unlock(&sb_lock);
	return count;
}

static ssize_t quota_pages_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%lu\n", READ_ONCE(SB_FROM_KOBJ(kobj)->quota_pages));
}
static ssize_t quota_pages_store(struct kobject *kobj, struct kobj_attribute *attr,
				 const char *buf, size_t count)
{
	unsigned long quota;
	unsigned long committed = 0;
	if (kstrtoul(buf, 10, &quota) || quota > node_present_pages(sandbox_fast_node))
		return -EINVAL;
	mutex_lock(&sb_lock);
	for (unsigned int i = 0; i < nr_sandboxes; i++)
		if (sandboxes[i] != SB_FROM_KOBJ(kobj))
			committed += sandboxes[i]->quota_pages;
	if (!sb_budget_pages || quota > sb_budget_pages - min(sb_budget_pages, committed)) {
		mutex_unlock(&sb_lock);
		return -ENOSPC;
	}
	demeter_alloc_update(SB_FROM_KOBJ(kobj)->alloc_rule, quota,
		SB_FROM_KOBJ(kobj)->fast_pages, SB_FROM_KOBJ(kobj)->phase, false);
	SB_FROM_KOBJ(kobj)->quota_pages = quota;
	if (sb_worker) wake_up_process(sb_worker);
	mutex_unlock(&sb_lock);
	return count;
}
static ssize_t residency_store(struct kobject *kobj, struct kobj_attribute *attr,
			       const char *buf, size_t count)
{
	struct sandbox *sb = SB_FROM_KOBJ(kobj);
	unsigned long fast, slow;
	u64 before = U64_MAX;
	if (sscanf(buf, "%lu %lu %llu", &fast, &slow, &before) < 2)
		return -EINVAL;
	mutex_lock(&sb_lock);
	sb->fast_pages = fast;
	sb->slow_pages = slow;
	demeter_alloc_feedback(sb->alloc_rule, fast, before);
	demeter_alloc_update(sb->alloc_rule, sb->quota_pages, fast, sb->phase, false);
	sb->feedback_ns = ktime_get_ns();
	mutex_unlock(&sb_lock);
	return count;
}
static ssize_t allocation_snapshot_show(struct kobject *kobj, struct kobj_attribute *attr,
		char *buf)
{
	struct demeter_alloc_stats a;
	mutex_lock(&sb_lock);
	demeter_alloc_stats(SB_FROM_KOBJ(kobj)->alloc_rule, &a);
	mutex_unlock(&sb_lock);
	return sysfs_emit(buf, "%llu\n", a.committed);
}
static ssize_t stats_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	struct sandbox *s = SB_FROM_KOBJ(kobj);
	ssize_t ret;
	mutex_lock(&sb_lock);
	ret = sysfs_emit(buf,
		"phase %u\nkind %u\nquota_pages %lu\nfast_pages %lu\nslow_pages %lu\n"
		"tasks %u\nregions %u\nsampled %llu\nstale %llu\nmetadata_overflow %llu\n"
		"ring_overruns %lu\nregistrations %llu\nperf_errors %llu\ncapacity_misses %llu\n"
		"decisions %llu\nhot_candidates %llu\ncold_candidates %llu\nrejected_benefit %llu\n"
		"promotions %llu\ndemotions %llu\nexchanges %llu\ncross_mm_exchanges %llu\n"
		"failed %llu\ninvalid %llu\ncopy_ns %llu\nworker_ns %llu\nworker_cpu_ns %llu\n"
		"sampling_ns %lld\nmigration_ns %llu\n",
		s->phase, s->kind, s->quota_pages, s->fast_pages, s->slow_pages,
		s->nr_tasks, s->nr_regions, s->sampled, s->stale, s->overflow,
		s->samples ? ring_buffer_overruns(s->samples) : 0UL,
		s->registrations, s->perf_errors, s->capacity_misses,
		s->decisions, s->hot_candidates, s->cold_candidates, s->rejected_benefit,
		s->promotions, s->demotions, s->exchanges, s->cross_mm_exchanges,
		s->failed, s->invalid, s->copy_ns, s->worker_ns, s->worker_cpu_ns,
		atomic64_read(&s->sampling_ns), s->migration_ns);
	ret += sysfs_emit_at(buf, ret,
		"placement_enabled %u\nload_overflows %lld\nstore_overflows %lld\ntid_mm_rejections %lld\ncross_mm_enabled %u\nfile_demotion_enabled %u\n"
		"old_gate_profitable_pairs %llu\nold_gate_cross_pairs %llu\nrelative_exchanges %llu\n"
		"file_demotions %llu\nfile_failed %llu\nfile_scanned %llu\nfile_eligible %llu\nfile_skipped %llu\n",
		sandbox_placement_enabled, atomic64_read(&s->captured_load),
		atomic64_read(&s->captured_store), atomic64_read(&s->rejected_tid), sandbox_cross_mm, sandbox_file_demotion, s->old_gate_profitable_pairs,
		s->old_gate_cross_pairs, s->relative_exchanges, s->file_demotions,
		s->file_failed, s->file_scanned, s->file_eligible, s->file_skipped);
	ret += sysfs_emit_at(buf, ret, "exchange_cost_ns %llu\nplacement_ns %llu\nsmoothed_heat %u\n",
		s->exchange_cost_ns, s->placement_ns, sandbox_smoothed_heat);
	ret += sysfs_emit_at(buf, ret, "load_event %u\n", sandbox_load_event);
	ret += sysfs_emit_at(buf, ret,
		"heat_shift %u\nconfidence_prior %u\nload_weighted %llu\nstore_weighted %llu\n"
		"wait_batch_pages %u\nreclaim_timeouts %llu\nmetadata_limit %u\n",
		sb_heat_shift(), sandbox_confidence_prior, s->load_weighted,
		s->store_weighted, sandbox_wait_batch_pages, s->reclaim_timeouts, SB_REGION_MAX);
	ret += sysfs_emit_at(buf, ret, "skip_unprofitable_scan %u\nunprofitable_scan_skips %llu\n",
		sandbox_skip_unprofitable_scan, s->unprofitable_scan_skips);
	ret += sysfs_emit_at(buf, ret, "lifetime_kind_assignments %llu\n", s->lifetime_kind_assignments);
	ret += sysfs_emit_at(buf, ret,
		"sample_record_bytes %zu\nsample_ring_bytes %u\nsample_format_errors %llu\n",
		sb_sample_bytes(), SB_RING_BYTES, s->sample_format_errors);
	ret += sysfs_emit_at(buf, ret, "sampling_enabled %u\n", sandbox_sampling_enabled);
	ret += sysfs_emit_at(buf, ret,
		"registration_lifecycle_skips %llu\nregistration_partial_unwinds %llu\n",
		s->registration_lifecycle_skips, s->registration_partial_unwinds);
	ret += sysfs_emit_at(buf, ret, "fast_node %d\nslow_node %d\nfuture_benefit %u\n",
		sandbox_fast_node, sandbox_slow_node, sandbox_future_benefit);
	ret += sysfs_emit_at(buf, ret, "sample_batch_enabled %u\nsamples_received %llu\nsample_cache_hits %llu\nsample_drain_ns %llu\nsample_budget_hits %llu\nsample_max_age_ns %llu\n",
		sandbox_sample_batch, s->samples_received, s->sample_cache_hits, s->sample_drain_ns, s->sample_budget_hits, s->sample_max_age_ns);
	ret += sysfs_emit_at(buf, ret, "sample_wakeup_enabled %u\nsample_wakeups %lld\n", sandbox_sample_wakeup, atomic64_read(&s->sample_wakeups));
	ret += sysfs_emit_at(buf, ret, "sample_wake_us %u\nlifecycle_wakeup_enabled %u\nlifecycle_fork_notifications %lld\nlifecycle_exec_notifications %lld\nlifecycle_discoveries %lld\n",
		sandbox_sample_wake_us, sandbox_lifecycle_wakeup,
		atomic64_read(&sb_fork_notifications), atomic64_read(&sb_exec_notifications),
		atomic64_read(&sb_lifecycle_discoveries));
	ret += sysfs_emit_at(buf, ret, "phase_seq %llu\nphase_start_ns %llu\ndeadline_ns %llu\n",
		s->phase_seq, s->phase_start_ns, s->deadline_ns);
	ret += sysfs_emit_at(buf, ret, "load_sample_period %lu\nbackoff_entries %u\nbackoff_limited %llu\n",
		sandbox_load_sample_period, s->nr_backoff, s->backoff_limited);
	{
		struct demeter_alloc_stats a;
		demeter_alloc_stats(s->alloc_rule, &a);
		ret += sysfs_emit_at(buf, ret, "allocation_enabled %u\nallocation_fast %llu\nallocation_slow %llu\nallocation_failed %llu\nallocation_fallback %llu\nallocation_native_fallback %llu\nallocation_remaining %lu\n",
			sandbox_allocation_enabled, a.fast, a.slow, a.failed, a.fallback, a.native_fallback, a.remaining);
		ret += sysfs_emit_at(buf, ret,
			"allocation_recycle_enabled %u\nallocation_committed %llu\nallocation_released %llu\nallocation_recycled %llu\nallocation_refunded %llu\nallocation_tracking_failed %llu\nallocation_balance %lu\nallocation_live_fast %lu\nallocation_inflight %lu\nallocation_live_peak %lu\nallocation_ceiling %lu\n",
			sandbox_recycle_credit, a.committed, a.released, a.recycled, a.refunded,
			a.tracking_failed, a.balance, a.live, a.inflight, a.live_peak, a.ceiling);
	}
	mutex_unlock(&sb_lock);
	return ret;
}
/* One consumer drains the bounded FIFO. Task names are hexadecimal so the
 * CSV cannot be corrupted by commas/newlines in user-chosen comm strings. */
static ssize_t events_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
 struct sandbox *sb = SB_FROM_KOBJ(kobj);
 int n = 0;
 mutex_lock(&sb_lock);
 while (sb->diag && sb->diag_tail != sb->diag_head && PAGE_SIZE - n >= 768) {
  struct sb_diag_record *r = &sb->diag[sb->diag_tail++ % SB_DIAG_MAX];
  n += sysfs_emit_at(buf, n, "%llu,%llu,%llu,%llu,%u,%u,%u,%llu,%llu,%*phN,%*phN",
   r->time_ns, r->serial, r->decision, r->phase_seq, r->phase, r->kind,
   r->event, r->generation, r->peer_generation, TASK_COMM_LEN, r->comm,
   TASK_COMM_LEN, r->peer_comm);
  for (unsigned int i=0; i<r->n; i++) n += sysfs_emit_at(buf, n, ",%llu", r->values[i]);
  n += sysfs_emit_at(buf, n, "\n");
 }
 mutex_unlock(&sb_lock);
 return n;
}
static ssize_t diagnostics_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
 struct sandbox *sb = SB_FROM_KOBJ(kobj);
 int n;
 mutex_lock(&sb_lock);
 n = sysfs_emit(buf, "enabled %u\nevents %llu\ndropped %llu\npending %u\n",
   sandbox_diagnostics, sb->diag_serial, sb->diag_dropped, sb->diag_head-sb->diag_tail);
 for (unsigned int r=0; r<SB_RESOLVE_ROLES; r++)
  for (unsigned int why=0; why<SB_RESOLVE_REASONS; why++)
   n += sysfs_emit_at(buf,n,"resolve_%u_%u %llu\n",r,why,sb->resolve[r][why]);
 mutex_unlock(&sb_lock);
 return n;
}
static struct kobj_attribute sb_events = __ATTR(events, 0400, events_show, NULL);
static struct kobj_attribute sb_diagnostics = __ATTR_RO(diagnostics);
static struct kobj_attribute sb_cgroup = __ATTR_RW_MODE(cgroup, 0600);
static struct kobj_attribute sb_phase = __ATTR_RW_MODE(phase, 0600);
static struct kobj_attribute sb_horizon = __ATTR(horizon_ms, 0200, NULL, horizon_ms_store);
static struct kobj_attribute sb_quota = __ATTR_RW_MODE(quota_pages, 0600);
static struct kobj_attribute sb_alloc_snapshot = __ATTR_RO_MODE(allocation_snapshot, 0400);
static struct kobj_attribute sb_residency = __ATTR(residency, 0200, NULL, residency_store);
static struct kobj_attribute sb_stats = __ATTR_RO(stats);
static struct attribute *sb_attrs[] = {
	&sb_cgroup.attr, &sb_phase.attr, &sb_horizon.attr, &sb_quota.attr, &sb_residency.attr, &sb_alloc_snapshot.attr, &sb_stats.attr, &sb_events.attr, &sb_diagnostics.attr, NULL,
};
ATTRIBUTE_GROUPS(sb);
static const struct kobj_type sb_ktype = {
	.release = sb_release, .sysfs_ops = &kobj_sysfs_ops, .default_groups = sb_groups,
};
static ssize_t nr_sandboxes_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%u\n", READ_ONCE(nr_sandboxes));
}
static ssize_t nr_sandboxes_store(struct kobject *kobj, struct kobj_attribute *attr,
				  const char *buf, size_t count)
{
	unsigned int n;
	struct sandbox *retired[SB_MAX];
	unsigned int nr_retired = 0;
	int err = 0;
	if (kstrtouint(buf, 10, &n) || n > SB_MAX)
		return -EINVAL;
	/* Switching policy requires clearing legacy slots first. */
	mutex_lock(&demeter_sysfs_lock);
	mutex_lock(&sb_lock);
	if (n && (nr_sandboxes || demeter_legacy_enabled())) {
		err = -EBUSY;
		goto out;
	}
	if (!n) {
		while (nr_sandboxes)
			retired[nr_retired++] = sandboxes[--nr_sandboxes];
		goto out;
	}
	for (unsigned int i = 0; i < n; i++) {
		struct sandbox *sb = kzalloc(sizeof(*sb), GFP_KERNEL);
		if (!sb) {
			err = -ENOMEM;
			break;
		}
		if (sandbox_diagnostics) {
   sb->diag = vzalloc(sizeof(*sb->diag) * SB_DIAG_MAX);
   if (!sb->diag) { kfree(sb); err = -ENOMEM; break; }
  }
  sb->id = i;
  sb->copy_ns = sandbox_initial_copy_ns;
  sb->exchange_cost_ns = 2 * sandbox_initial_copy_ns;
		sb->phase_start_ns = ktime_get_ns();
		INIT_LIST_HEAD(&sb->tasks);
		INIT_LIST_HEAD(&sb->spaces);
		init_irq_work(&sb->sample_wake, sb_sample_wake);
		sb->samples = mpsc_new(SB_RING_BYTES);
		if (!sb->samples) {
   vfree(sb->diag);
   kfree(sb);
			err = -ENOMEM;
			break;
		}
		err = kobject_init_and_add(&sb->kobj, &sb_ktype, sb_root, "%u", i);
		if (err) {
			retired[nr_retired++] = sb;
			break;
		}
		sandboxes[nr_sandboxes++] = sb;
	}
	if (err)
		while (nr_sandboxes)
			retired[nr_retired++] = sandboxes[--nr_sandboxes];
out:
	mutex_unlock(&sb_lock);
	/* kernfs removal waits for active show/store callbacks. They take
	 * sb_lock, so it must be released before putting any published kobject.
	 * demeter_sysfs_lock keeps mode changes serialized during retirement.
	 */
	for (unsigned int i = 0; i < nr_retired; i++)
		kobject_put(&retired[i]->kobj);
	mutex_unlock(&demeter_sysfs_lock);
	return err ? err : count;
}
static struct kobj_attribute sb_nr = __ATTR_RW_MODE(nr_sandboxes, 0600);
static ssize_t budget_pages_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%lu\n", READ_ONCE(sb_budget_pages));
}
static ssize_t budget_pages_store(struct kobject *kobj, struct kobj_attribute *attr,
				 const char *buf, size_t count)
{
	unsigned long budget, committed = 0;
	if (kstrtoul(buf, 10, &budget) || budget > node_present_pages(sandbox_fast_node))
		return -EINVAL;
	mutex_lock(&sb_lock);
	for (unsigned int i = 0; i < nr_sandboxes; i++)
		committed += sandboxes[i]->quota_pages;
	if (budget < committed) {
		mutex_unlock(&sb_lock);
		return -EBUSY;
	}
	sb_budget_pages = budget;
	mutex_unlock(&sb_lock);
	return count;
}
static struct kobj_attribute sb_budget = __ATTR_RW_MODE(budget_pages, 0600);

int sandbox_sysfs_init(struct kobject *root)
{
	int err;
	if (sandbox_fast_node < 0) sandbox_fast_node = FMEM_NID;
	if (sandbox_slow_node < 0) sandbox_slow_node = SMEM_NID;
	if (sandbox_fast_node >= MAX_NUMNODES || sandbox_slow_node >= MAX_NUMNODES ||
	    sandbox_fast_node == sandbox_slow_node ||
	    !node_state(sandbox_fast_node, N_MEMORY) ||
	    !node_state(sandbox_slow_node, N_MEMORY))
		return -EINVAL;
	if (!sandbox_period_ms || sandbox_period_ms > 1000 ||
	    !sandbox_scan_pages || sandbox_scan_pages > 65536 ||
	    !sandbox_batch_pages || sandbox_batch_pages > SB_POOL_MAX ||
	    !sandbox_wait_batch_pages || sandbox_wait_batch_pages > SB_FILE_POOL_MAX ||
	    !sandbox_reclaim_time_ms || sandbox_reclaim_time_ms > 20 ||
	    sandbox_confidence_prior > 1000 ||
	    sandbox_load_weight_permille > 100000 ||
	    sandbox_store_weight_permille > 100000 ||
	    sandbox_load_sample_period < 1024 || sandbox_load_sample_period > 1000000 ||
	    (sandbox_load_event != 0x81d0 && sandbox_load_event != 0x01cd))
		return -EINVAL;
	sb_root = kobject_create_and_add("sandboxes", root);
	if (!sb_root)
		return -ENOMEM;
	err = sysfs_create_file(sb_root, &sb_nr.attr);
	if (err)
		goto fail;
	err = sysfs_create_file(sb_root, &sb_budget.attr);
	if (err)
		goto fail;
	sb_worker = kthread_run(sb_work, NULL, "demeter-sandbox");
	if (IS_ERR(sb_worker)) {
		err = PTR_ERR(sb_worker);
		sb_worker = NULL;
		goto fail;
	}
	init_irq_work(&sb_lifecycle_work, sb_lifecycle_wake);
	if (sandbox_lifecycle_wakeup) {
		for_each_kernel_tracepoint(sb_find_lifecycle_tp, NULL);
		if (!sb_fork_tp || !sb_exec_tp) { err = -ENOENT; goto fail_worker; }
		err = tracepoint_probe_register(sb_fork_tp, sb_note_fork, NULL);
		if (err) goto fail_worker;
		sb_fork_registered = true;
		err = tracepoint_probe_register(sb_exec_tp, sb_note_exec, NULL);
		if (err) goto fail_worker;
		sb_exec_registered = true;
	}
	return 0;
fail_worker:
	sb_lifecycle_unregister();
	{
		struct task_struct *worker = sb_worker;
		sb_worker = NULL;
		kthread_stop(worker);
	}
fail:
	kobject_put(sb_root);
	sb_root = NULL;
	return err;
}
void sandbox_sysfs_exit(void)
{
	struct sandbox *retired[SB_MAX];
	unsigned int count = 0;
	struct task_struct *worker;
	sb_lifecycle_unregister();
	if (sb_root)
		sysfs_remove_file(sb_root, &sb_nr.attr);
	mutex_lock(&sb_lock);
	{
		unsigned long flags;
		spin_lock_irqsave(&sb_wake_lock, flags);
		worker = sb_worker;
		sb_worker = NULL;
		spin_unlock_irqrestore(&sb_wake_lock, flags);
	}
	mutex_unlock(&sb_lock);
	if (worker)
		kthread_stop(worker);
	mutex_lock(&demeter_sysfs_lock);
	mutex_lock(&sb_lock);
	while (nr_sandboxes)
		retired[count++] = sandboxes[--nr_sandboxes];
	mutex_unlock(&sb_lock);
	for (unsigned int i = 0; i < count; i++)
		kobject_put(&retired[i]->kobj);
	mutex_unlock(&demeter_sysfs_lock);
	if (sb_root)
		kobject_put(sb_root);
}

MODULE_IMPORT_NS(DEMETER);
