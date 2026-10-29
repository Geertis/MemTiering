// SPDX-License-Identifier: GPL-2.0
#include <linux/mm.h>
#include <linux/mmzone.h>
#include <linux/page_reporting.h>
#include <linux/gfp.h>
#include <linux/export.h>
#include <linux/module.h>
#include <linux/delay.h>
#include <linux/scatterlist.h>

#include "page_reporting.h"
#include "internal.h"

/* Initialize to an unsupported value */
unsigned int page_reporting_order = -1;

static DEFINE_MUTEX(page_reporting_mutex);
static DEFINE_MUTEX(page_reporting_work_mutex);
static struct page_reporting_dev_info __rcu *pr_dev_info __read_mostly;
static bool page_reporting_paused;
static unsigned long long page_reporting_runs;
static unsigned long long page_reporting_batches;
static unsigned long long page_reporting_fine_batches;
static unsigned long long page_reporting_pause_wait_ns;
static unsigned long long page_reporting_barrier_wait_ns;

static int page_reporting_barrier_set(const char *val,
				      const struct kernel_param *kp)
{
	bool trigger;
	u64 started;
	int err = kstrtobool(val, &trigger);

	if (err || !trigger)
		return err;
	started = ktime_get_ns();
	/* The caller raises the floor first. Existing work must return its
	 * isolated pages; queued work takes its floor after this lock.
	 * Do not cancel or postpone coarse backing returns.
	 */
	mutex_lock(&page_reporting_work_mutex);
	WRITE_ONCE(page_reporting_barrier_wait_ns,
		   READ_ONCE(page_reporting_barrier_wait_ns) +
		   ktime_get_ns() - started);
	mutex_unlock(&page_reporting_work_mutex);
	return 0;
}

static const struct kernel_param_ops page_reporting_barrier_ops = {
	.set = page_reporting_barrier_set,
};
module_param_cb(page_reporting_barrier, &page_reporting_barrier_ops, NULL, 0200);
MODULE_PARM_DESC(page_reporting_barrier,
		"Wait for current reporting work without cancelling queued work");
module_param_named(page_reporting_barrier_wait_ns,
		   page_reporting_barrier_wait_ns, ullong, 0444);

static int page_reporting_pause_set(const char *val,
				    const struct kernel_param *kp)
{
	struct page_reporting_dev_info *prdev;
	bool pause;
	u64 started;
	int err = kstrtobool(val, &pause);

	if (err)
		return err;

	mutex_lock(&page_reporting_mutex);
	if (pause == READ_ONCE(page_reporting_paused))
		goto out;

	WRITE_ONCE(page_reporting_paused, pause);
	prdev = rcu_dereference_protected(pr_dev_info,
			lockdep_is_held(&page_reporting_mutex));
	if (pause && prdev) {
		started = ktime_get_ns();
		/* Finish old notification readers before cancelling their work.
		 * The worker returns all isolated pages before this write ACKs.
		 */
		synchronize_rcu_expedited();
		cancel_delayed_work_sync(&prdev->work);
		atomic_set(&prdev->state, 0);
		WRITE_ONCE(page_reporting_pause_wait_ns,
			READ_ONCE(page_reporting_pause_wait_ns) +
			ktime_get_ns() - started);
	} else if (!pause) {
		/* Previously free blocks may have no new free notification. */
		__page_reporting_notify();
	}
out:
	mutex_unlock(&page_reporting_mutex);
	return 0;
}

static const struct kernel_param_ops page_reporting_pause_ops = {
	.set = page_reporting_pause_set,
	.get = param_get_bool,
};
module_param_cb(page_reporting_paused, &page_reporting_pause_ops,
		&page_reporting_paused, 0644);
MODULE_PARM_DESC(page_reporting_paused,
		"Pause reporting and drain in-flight work before acknowledging");
module_param_named(page_reporting_runs, page_reporting_runs, ullong, 0444);
module_param_named(page_reporting_batches, page_reporting_batches, ullong, 0444);
module_param_named(page_reporting_fine_batches,
		   page_reporting_fine_batches, ullong, 0444);
MODULE_PARM_DESC(page_reporting_fine_batches,
		"Submitted batches containing a block smaller than order5");
module_param_named(page_reporting_pause_wait_ns,
		   page_reporting_pause_wait_ns, ullong, 0444);

static int page_order_update_notify(const char *val, const struct kernel_param *kp)
{
	unsigned int previous = READ_ONCE(page_reporting_order);
	int err;

	/*
	 * If param is set beyond this limit, order is set to default
	 * pageblock_order value
	 */
	err = param_set_uint_minmax(val, kp, 0, MAX_PAGE_ORDER);
	/* Existing small free blocks need a pass even without a new free. */
	if (!err && previous <= MAX_PAGE_ORDER &&
	    READ_ONCE(page_reporting_order) < previous)
		__page_reporting_notify();
	return err;
}

static const struct kernel_param_ops page_reporting_param_ops = {
	.set = &page_order_update_notify,
	/*
	 * For the get op, use param_get_int instead of param_get_uint.
	 * This is to make sure that when unset the initialized value of
	 * -1 is shown correctly
	 */
	.get = &param_get_int,
};

module_param_cb(page_reporting_order, &page_reporting_param_ops,
			&page_reporting_order, 0644);
MODULE_PARM_DESC(page_reporting_order, "Set page reporting order");

/*
 * This symbol is also a kernel parameter. Export the page_reporting_order
 * symbol so that other drivers can access it to control order values without
 * having to introduce another configurable parameter. Only one driver can
 * register with the page_reporting driver for the service, so we have just
 * one control parameter for the use case(which can be accessed in both
 * drivers)
 */
EXPORT_SYMBOL_GPL(page_reporting_order);

#define PAGE_REPORTING_DELAY	(2 * HZ)

enum {
	PAGE_REPORTING_IDLE = 0,
	PAGE_REPORTING_REQUESTED,
	PAGE_REPORTING_ACTIVE
};

/* request page reporting */
static void
__page_reporting_request(struct page_reporting_dev_info *prdev)
{
	unsigned int state;

	if (READ_ONCE(page_reporting_paused))
		return;

	/* Check to see if we are in desired state */
	state = atomic_read(&prdev->state);
	if (state == PAGE_REPORTING_REQUESTED)
		return;

	/*
	 * If reporting is already active there is nothing we need to do.
	 * Test against 0 as that represents PAGE_REPORTING_IDLE.
	 */
	state = atomic_xchg(&prdev->state, PAGE_REPORTING_REQUESTED);
	if (state != PAGE_REPORTING_IDLE)
		return;

	/*
	 * Delay the start of work to allow a sizable queue to build. For
	 * now we are limiting this to running no more than once every
	 * couple of seconds.
	 */
	schedule_delayed_work(&prdev->work, PAGE_REPORTING_DELAY);
}

/* notify prdev of free page reporting request */
void __page_reporting_notify(void)
{
	struct page_reporting_dev_info *prdev;

	/*
	 * We use RCU to protect the pr_dev_info pointer. In almost all
	 * cases this should be present, however in the unlikely case of
	 * a shutdown this will be NULL and we should exit.
	 */
	rcu_read_lock();
	prdev = rcu_dereference(pr_dev_info);
	if (likely(prdev))
		__page_reporting_request(prdev);

	rcu_read_unlock();
}

static void
page_reporting_drain(struct page_reporting_dev_info *prdev,
		     struct scatterlist *sgl, unsigned int nents, bool reported)
{
	struct scatterlist *sg = sgl;

	/*
	 * Drain the now reported pages back into their respective
	 * free lists/areas. We assume at least one page is populated.
	 */
	do {
		struct page *page = sg_page(sg);
		int mt = get_pageblock_migratetype(page);
		unsigned int order = get_order(sg->length);

		__putback_isolated_page(page, order, mt);

		/* If the pages were not reported due to error skip flagging */
		if (!reported)
			continue;

		/*
		 * If page was not comingled with another page we can
		 * consider the result to be "reported" since the page
		 * hasn't been modified, otherwise we will need to
		 * report on the new larger page when we make our way
		 * up to that higher order.
		 */
		if (PageBuddy(page) && buddy_order(page) == order)
			__SetPageReported(page);
	} while ((sg = sg_next(sg)));

	/* reinitialize scatterlist now that it is empty */
	sg_init_table(sgl, nents);
}

static int page_reporting_submit(struct page_reporting_dev_info *prdev,
				 struct scatterlist *sgl, unsigned int nents)
{
	struct scatterlist *sg;
	unsigned int i;
	bool fine = false;

	for_each_sg(sgl, sg, nents, i) {
		if (sg->length < (PAGE_SIZE << 5)) {
			fine = true;
			break;
		}
	}
	WRITE_ONCE(page_reporting_batches, READ_ONCE(page_reporting_batches) + 1);
	if (fine)
		WRITE_ONCE(page_reporting_fine_batches,
			   READ_ONCE(page_reporting_fine_batches) + 1);
	return prdev->report(prdev, sgl, nents);
}

/*
 * The page reporting cycle consists of 4 stages, fill, report, drain, and
 * idle. We will cycle through the first 3 stages until we cannot obtain a
 * full scatterlist of pages, in that case we will switch to idle.
 */
static int
page_reporting_cycle(struct page_reporting_dev_info *prdev, struct zone *zone,
		     unsigned int order, unsigned int mt,
		     struct scatterlist *sgl, unsigned int *offset)
{
	struct free_area *area = &zone->free_area[order];
	struct list_head *list = &area->free_list[mt];
	unsigned int page_len = PAGE_SIZE << order;
	struct page *page, *next;
	long budget;
	int err = 0;

	/*
	 * Perform early check, if free area is empty there is
	 * nothing to process so we can skip this free_list.
	 */
	if (list_empty(list))
		return err;

	spin_lock_irq(&zone->lock);

	/*
	 * Limit how many calls we will be making to the page reporting
	 * device for this list. By doing this we avoid processing any
	 * given list for too long.
	 *
	 * The current value used allows us enough calls to process over a
	 * sixteenth of the current list plus one additional call to handle
	 * any pages that may have already been present from the previous
	 * list processed. This should result in us reporting all pages on
	 * an idle system in about 30 seconds.
	 *
	 * The division here should be cheap since PAGE_REPORTING_CAPACITY
	 * should always be a power of 2.
	 */
	budget = DIV_ROUND_UP(area->nr_free, PAGE_REPORTING_CAPACITY * 16);

	/* loop through free list adding unreported pages to sg list */
	list_for_each_entry_safe(page, next, list, lru) {
		/* A resumed tool can raise the floor during a low-order pass.
		 * Finish already isolated pages, but stop taking small blocks.
		 */
		if (READ_ONCE(page_reporting_paused) ||
		    order < READ_ONCE(page_reporting_order))
			break;

		/* We are going to skip over the reported pages. */
		if (PageReported(page))
			continue;

		/*
		 * If we fully consumed our budget then update our
		 * state to indicate that we are requesting additional
		 * processing and exit this list.
		 */
		if (budget < 0) {
			atomic_set(&prdev->state, PAGE_REPORTING_REQUESTED);
			next = page;
			break;
		}

		/* Attempt to pull page from list and place in scatterlist */
		if (*offset) {
			if (!__isolate_free_page(page, order)) {
				next = page;
				break;
			}

			/* Add page to scatter list */
			--(*offset);
			sg_set_page(&sgl[*offset], page, page_len, 0);

			continue;
		}

		/*
		 * Make the first non-reported page in the free list
		 * the new head of the free list before we release the
		 * zone lock.
		 */
		if (!list_is_first(&page->lru, list))
			list_rotate_to_front(&page->lru, list);

		/* release lock before waiting on report processing */
		spin_unlock_irq(&zone->lock);

		/* begin processing pages in local list */
		err = page_reporting_submit(prdev, sgl, PAGE_REPORTING_CAPACITY);

		/* reset offset since the full list was reported */
		*offset = PAGE_REPORTING_CAPACITY;

		/* update budget to reflect call to report function */
		budget--;

		/* reacquire zone lock and resume processing */
		spin_lock_irq(&zone->lock);

		/* flush reported pages from the sg list */
		page_reporting_drain(prdev, sgl, PAGE_REPORTING_CAPACITY, !err);

		/*
		 * Reset next to first entry, the old next isn't valid
		 * since we dropped the lock to report the pages
		 */
		next = list_first_entry(list, struct page, lru);

		/* exit on error */
		if (err)
			break;
	}

	/* Rotate any leftover pages to the head of the freelist */
	if (!list_entry_is_head(next, list, lru) && !list_is_first(&next->lru, list))
		list_rotate_to_front(&next->lru, list);

	spin_unlock_irq(&zone->lock);

	return err;
}

static int
page_reporting_process_zone(struct page_reporting_dev_info *prdev,
			    struct scatterlist *sgl, struct zone *zone,
			    unsigned int min_order)
{
	unsigned int order, mt, leftover, offset = PAGE_REPORTING_CAPACITY;
	unsigned long watermark;
	int err = 0;

	/* Generate minimum watermark to be able to guarantee progress */
	watermark = low_wmark_pages(zone) +
		    (PAGE_REPORTING_CAPACITY << min_order);

	/*
	 * Cancel request if insufficient free memory or if we failed
	 * to allocate page reporting statistics for the zone.
	 */
	if (!zone_watermark_ok(zone, 0, watermark, 0, ALLOC_CMA))
		return err;

	/* Process each free list starting from lowest order/mt */
	for (order = min_order; order < NR_PAGE_ORDERS; order++) {
		for (mt = 0; mt < MIGRATE_TYPES; mt++) {
			if (READ_ONCE(page_reporting_paused))
				goto drain_leftover;
			/* We do not pull pages from the isolate free list */
			if (is_migrate_isolate(mt))
				continue;

			err = page_reporting_cycle(prdev, zone, order, mt,
						   sgl, &offset);
			if (err)
				return err;
		}
	}

drain_leftover:
	/* Even while pausing, return every already isolated page. */
	leftover = PAGE_REPORTING_CAPACITY - offset;
	if (leftover) {
		sgl = &sgl[offset];
		err = page_reporting_submit(prdev, sgl, leftover);

		/* flush any remaining pages out from the last report */
		spin_lock_irq(&zone->lock);
		page_reporting_drain(prdev, sgl, leftover, !err);
		spin_unlock_irq(&zone->lock);
	}

	return err;
}

static void page_reporting_process(struct work_struct *work)
{
	struct delayed_work *d_work = to_delayed_work(work);
	struct page_reporting_dev_info *prdev =
		container_of(d_work, struct page_reporting_dev_info, work);
	int err = 0, state = PAGE_REPORTING_ACTIVE;
	struct scatterlist *sgl;
	struct zone *zone;
	unsigned int min_order;

	mutex_lock(&page_reporting_work_mutex);
	min_order = READ_ONCE(page_reporting_order);

	if (READ_ONCE(page_reporting_paused)) {
		atomic_set(&prdev->state, PAGE_REPORTING_IDLE);
		mutex_unlock(&page_reporting_work_mutex);
		return;
	}
	WRITE_ONCE(page_reporting_runs, READ_ONCE(page_reporting_runs) + 1);

	/*
	 * Change the state to "Active" so that we can track if there is
	 * anyone requests page reporting after we complete our pass. If
	 * the state is not altered by the end of the pass we will switch
	 * to idle and quit scheduling reporting runs.
	 */
	atomic_set(&prdev->state, state);

	/* allocate scatterlist to store pages being reported on */
	sgl = kmalloc_array(PAGE_REPORTING_CAPACITY, sizeof(*sgl), GFP_KERNEL);
	if (!sgl)
		goto err_out;

	sg_init_table(sgl, PAGE_REPORTING_CAPACITY);

	for_each_zone(zone) {
		if (READ_ONCE(page_reporting_paused))
			break;
		err = page_reporting_process_zone(prdev, sgl, zone, min_order);
		if (err)
			break;
	}

	kfree(sgl);
err_out:
	/*
	 * If the state has reverted back to requested then there may be
	 * additional pages to be processed. We will defer for 2s to allow
	 * more pages to accumulate.
	 */
	state = atomic_cmpxchg(&prdev->state, state, PAGE_REPORTING_IDLE);
	if (state == PAGE_REPORTING_REQUESTED &&
	    !READ_ONCE(page_reporting_paused))
		schedule_delayed_work(&prdev->work, PAGE_REPORTING_DELAY);
	mutex_unlock(&page_reporting_work_mutex);
}

DEFINE_STATIC_KEY_FALSE(page_reporting_enabled);

int page_reporting_register(struct page_reporting_dev_info *prdev)
{
	int err = 0;

	mutex_lock(&page_reporting_mutex);

	/* nothing to do if already in use */
	if (rcu_dereference_protected(pr_dev_info,
				lockdep_is_held(&page_reporting_mutex))) {
		err = -EBUSY;
		goto err_out;
	}

	/*
	 * If the page_reporting_order value is not set, we check if
	 * an order is provided from the driver that is performing the
	 * registration. If that is not provided either, we default to
	 * pageblock_order.
	 */

	if (page_reporting_order == -1) {
		if (prdev->order > 0 && prdev->order <= MAX_PAGE_ORDER)
			page_reporting_order = prdev->order;
		else
			page_reporting_order = pageblock_order;
	}

	/* initialize state and work structures */
	atomic_set(&prdev->state, PAGE_REPORTING_IDLE);
	INIT_DELAYED_WORK(&prdev->work, &page_reporting_process);

	/* Begin initial flush of zones */
	__page_reporting_request(prdev);

	/* Assign device to allow notifications */
	rcu_assign_pointer(pr_dev_info, prdev);

	/* enable page reporting notification */
	if (!static_key_enabled(&page_reporting_enabled)) {
		static_branch_enable(&page_reporting_enabled);
		pr_info("Free page reporting enabled\n");
	}
err_out:
	mutex_unlock(&page_reporting_mutex);

	return err;
}
EXPORT_SYMBOL_GPL(page_reporting_register);

void page_reporting_unregister(struct page_reporting_dev_info *prdev)
{
	mutex_lock(&page_reporting_mutex);

	if (prdev == rcu_dereference_protected(pr_dev_info,
				lockdep_is_held(&page_reporting_mutex))) {
		/* Disable page reporting notification */
		RCU_INIT_POINTER(pr_dev_info, NULL);
		synchronize_rcu();

		/* Flush any existing work, and lock it out */
		cancel_delayed_work_sync(&prdev->work);
	}

	mutex_unlock(&page_reporting_mutex);
}
EXPORT_SYMBOL_GPL(page_reporting_unregister);
