#include <linux/module.h>

#include "PhaseTieing.h"
#include "module.h"

ulong load_latency_sample_period = LOAD_LATENCY_SAMPLE_PERIOD;
module_param_named(load_latency_sample_period, load_latency_sample_period,
		   ulong, 0644);
MODULE_PARM_DESC(load_latency_sample_period,
		 "Sample period for ldlat event, defaults to 17");

ulong load_latency_threshold = LOAD_LATENCY_THRESHOLD;
module_param_named(load_latency_threshold, load_latency_threshold, ulong, 0644);
MODULE_PARM_DESC(load_latency_threshold,
		 "Load latency threshold for ldlat event, defaults to 64");

ulong retired_stores_sample_period = RETIRED_STORES_SAMPLE_PERIOD;
module_param_named(retired_stores_sample_period, retired_stores_sample_period,
		   ulong, 0644);
MODULE_PARM_DESC(retired_stores_sample_period,
		 "Sample period for retired stores event, defaults to 17");

ulong load_l3_miss_sample_period = LOAD_L3_MISS_SAMPLE_PERIOD;
module_param_named(load_l3_miss_sample_period, load_l3_miss_sample_period,
		   ulong, 0644);
MODULE_PARM_DESC(load_l3_miss_sample_period,
		 "Sample period for local DRAM L3 miss event, defaults to 17");

ulong throttle_pulse_width_ms = THROTTLE_PULSE_WIDTH_MS;
module_param_named(throttle_pulse_width_ms, throttle_pulse_width_ms, ulong,
		   0644);
MODULE_PARM_DESC(throttle_pulse_width_ms,
		 "Throttle pulse width in ms, defaults to 1000");

ulong throttle_pulse_period_ms = THROTTLE_PULSE_PERIOD_MS;
module_param_named(throttle_pulse_period_ms, throttle_pulse_period_ms, ulong,
		   0644);
MODULE_PARM_DESC(throttle_pulse_period_ms,
		 "Throttle pulse period in ms, defaults to 5000");

ulong split_period_ms = SPLI_PERIOD_MS;
module_param_named(split_period_ms, split_period_ms, ulong, 0644);
MODULE_PARM_DESC(split_period_ms, "Split period in ms, defaults to 500");

ulong rtree_split_thresh = RTREE_SPLIT_THRESH;
module_param_named(rtree_split_thresh, rtree_split_thresh, ulong, 0644);
MODULE_PARM_DESC(rtree_split_thresh,
		 "Split threshold in bytes, defaults to 15");

ulong rtree_exch_thresh = RTREE_EXCH_THRESH;
module_param_named(rtree_exch_thresh, rtree_exch_thresh, ulong, 0644);
MODULE_PARM_DESC(rtree_exch_thresh,
		 "Exchange threshold in bytes, defaults to 2 MiB");

/* Optional range initialization for short-lived, phase-oriented workloads. */
ulong initial_range_size_mb;
module_param_named(initial_range_size_mb, initial_range_size_mb, ulong, 0644);
MODULE_PARM_DESC(initial_range_size_mb,
		 "Initial heap/mmap range coverage in MiB; 0 uses total RAM");

bool adaptive_range;
module_param_named(adaptive_range, adaptive_range, bool, 0644);
MODULE_PARM_DESC(adaptive_range,
		 "Size initial ranges from the target address-space footprint");

bool candidate_trace;
module_param_named(candidate_trace, candidate_trace, bool, 0644);
MODULE_PARM_DESC(candidate_trace,
		 "Log per-PID hot slow and cold fast candidate counts in pages");

DEFINE_STATIC_KEY_TRUE(should_decay_sketch);
struct kmem_cache *list_head_cache;

static void intel_pmu_print_debug_all(void)
{
	int cpu;
	for_each_online_cpu(cpu) {
		void perf_event_print_debug(void);
		smp_call_on_cpu(cpu, (int (*)(void *))perf_event_print_debug,
				NULL, false);
	}
}

struct perf_event_attr event_attrs[MAX_EVENTS] = {
	[EVENT_LOAD] = {
		.type = PERF_TYPE_RAW,
		.config = MEM_TRANS_RETIRED_LOAD_LATENCY,
		.config1 = LOAD_LATENCY_THRESHOLD,
		.sample_type = PERF_SAMPLE_TID | PERF_SAMPLE_TIME | PERF_SAMPLE_ADDR |
			       PERF_SAMPLE_WEIGHT | PERF_SAMPLE_PHYS_ADDR,
		.sample_period = LOAD_LATENCY_SAMPLE_PERIOD ,
		.inherit = 1,
		/* A target is one process (all threads), not its child processes. */
		.inherit_thread = 1,
		/* Xeon 6530P rejects per-task load-latency events at precise_ip=3. */
		.precise_ip = 2,
		// .disabled = 1,
		.exclude_kernel = 1,
		.exclude_hv = 1,
		.exclude_callchain_kernel = 1,
	},
	// [EVENT_LOAD] = {
	// 	.type = PERF_TYPE_RAW,
	// 	.config = MEM_LOAD_L3_MISS_RETIRED_LOCAL_DRAM,
	// 	.sample_type = PERF_SAMPLE_TID | PERF_SAMPLE_TIME | PERF_SAMPLE_ADDR |
	// 		       PERF_SAMPLE_WEIGHT | PERF_SAMPLE_PHYS_ADDR,
	// 	.sample_period = LOAD_L3_MISS_SAMPLE_PERIOD,
	// 	.inherit = 1,
	// 	.precise_ip = 3,
	// 	// .disabled = 1,
	// 	.exclude_kernel = 1,
	// 	.exclude_hv = 1,
	// 	.exclude_callchain_kernel = 1,
	// },
	[EVENT_STORE] = {
		.type = PERF_TYPE_RAW,
		.config = MEM_INST_RETIRED_ALL_STORES,
		.sample_type = PERF_SAMPLE_TID | PERF_SAMPLE_TIME | PERF_SAMPLE_ADDR |
			       PERF_SAMPLE_WEIGHT | PERF_SAMPLE_PHYS_ADDR,
		.sample_period = RETIRED_STORES_SAMPLE_PERIOD ,
		.inherit = 1,
		.inherit_thread = 1,
		.precise_ip = 3,
		// .disabled = 1,
		.exclude_kernel = 1,
		.exclude_hv = 1,
		.exclude_callchain_kernel = 1,
	},
};
static inline void event_attrs_update_param(void)
{
	event_attrs[EVENT_LOAD].sample_period = load_latency_sample_period;
	event_attrs[EVENT_LOAD].config1 = load_latency_threshold;

	// event_attrs[EVENT_LOAD].sample_period = load_l3_miss_sample_period;
	event_attrs[EVENT_STORE].sample_period = retired_stores_sample_period;
	pr_info("%s: local_dram_miss_sample_period=%lu retired_stores_sample_period=%lu load_latency_sample_period=%lu load_latency_threshold=%lu\n",
		__func__, load_l3_miss_sample_period,
		retired_stores_sample_period, load_latency_sample_period,
		load_latency_threshold);
}

static u64 num_possible_pages(void)
{
	u64 spanned = 0;
	int nid;
	for_each_node_state(nid, N_MEMORY) {
		spanned += node_spanned_pages(nid);
	}
	return spanned;
}

static __init int init(void)
{
	list_head_cache = KMEM_CACHE(list_head, 0);
	if (!list_head_cache) {
		pr_err("Failed to create list_head cache\n");
		return -ENOMEM;
	}
	event_attrs_update_param();
	return demeter_sysfs_init();
}

static __exit void exit(void)
{
	demeter_sysfs_exit();
	kmem_cache_destroy(list_head_cache);
}

module_init(init);
module_exit(exit);
MODULE_AUTHOR("Junliang Hu <jlhu@cse.cuhk.edu.hk>");
MODULE_DESCRIPTION("Memory placement optimization module");
MODULE_LICENSE("GPL");
