// Copyright (c) 2020 Ant Financial
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

use crate::{
    seccomp_filters::Thread, thread_helper::spawn_virtio_thread, ActivateError, ActivateResult,
    EpollHelper, EpollHelperError, EpollHelperHandler, GuestMemoryMmap, VirtioCommon, VirtioDevice,
    VirtioDeviceType, VirtioInterrupt, VirtioInterruptType, EPOLL_HELPER_EVENT_LAST,
    VIRTIO_F_VERSION_1,
};
use anyhow::anyhow;
use seccompiler::SeccompAction;
use std::collections::HashMap;
use std::io::{self, Write};
use std::mem::size_of;
use std::num::Wrapping;
use std::ops::Index;
use std::os::unix::io::AsRawFd;
use std::result;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{atomic::AtomicBool, Arc, Barrier};
use std::time::Duration;
use thiserror::Error;
use versionize::{VersionMap, Versionize, VersionizeResult};
use versionize_derive::Versionize;
use virtio_queue::{Queue, QueueT};
use vm_allocator::page_size::{align_page_size_down, get_page_size};
use vm_memory::{
    Address, ByteValued, Bytes, GuestAddress, GuestAddressSpace, GuestMemory, GuestMemoryAtomic,
    GuestMemoryError, GuestMemoryRegion,
};
use vm_migration::{
    Migratable, MigratableError, Pausable, Snapshot, Snapshottable, Transportable, VersionMapped,
};
use vmm_sys_util::{eventfd::EventFd, timerfd::TimerFd};

const QUEUE_SIZE: u16 = 128;
const STATS_QUEUE_SIZE: u16 = 32;
const REPORTING_QUEUE_SIZE: u16 = 32;
const MIN_NUM_QUEUES: usize = 2;

// Inflate virtio queue event.
const INFLATE_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 1;
// Deflate virtio queue event.
const DEFLATE_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 2;
// Memory statistics virtio queue event.
const STATS_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 3;
// The time interval during two memory stat requests expires.
const STATS_TIMER_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 4;
// Reporting virtio queue event.
const REPORTING_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 5;
// Heterogeneous inflate virtio queue event.
const HETERO_INFLATE_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 6;
// Heterogeneous deflate virtio queue event.
const HETERO_DEFLATE_QUEUE_EVENT: u16 = EPOLL_HELPER_EVENT_LAST + 7;

// Size of a PFN in the balloon interface.
const VIRTIO_BALLOON_PFN_SHIFT: u64 = 12;

// Memory statistics virtqueue
const VIRTIO_BALLOON_F_STATS_VQ: u64 = 1;
// Deflate balloon on OOM
const VIRTIO_BALLOON_F_DEFLATE_ON_OOM: u64 = 2;
// Enable an additional virtqueue to let the guest notify the host about free
// pages.
const VIRTIO_BALLOON_F_REPORTING: u64 = 5;
// Enable an additional pair of inflate and deflate virtqueues to handle ballooning of heterogeneous memory
const VIRTIO_BALLOON_F_HETERO_MEM: u64 = 6;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
enum BalloonVq {
    Inflate,
    Deflate,
    Stats,
    // Not supported currently
    _FreePage,
    Reporting,
    HeteroInflate,
    HeteroDeflate,
}

#[derive(Error, Debug)]
pub enum Error {
    #[error("Guest gave us bad memory addresses.: {0}")]
    GuestMemory(GuestMemoryError),
    #[error("Guest gave us a write only descriptor that protocol says to read from")]
    UnexpectedWriteOnlyDescriptor,
    #[error("Guest sent us invalid request")]
    InvalidRequest,
    #[error("Fallocate fail.: {0}")]
    FallocateFail(std::io::Error),
    #[error("Madvise fail.: {0}")]
    MadviseFail(std::io::Error),
    #[error("Failed to EventFd write.: {0}")]
    EventFdWriteFail(std::io::Error),
    #[error("Invalid queue index: {0}")]
    InvalidQueueIndex(usize),
    #[error("Fail tp signal: {0}")]
    FailedSignal(io::Error),
    #[error("Descriptor chain is too short")]
    DescriptorChainTooShort,
    #[error("Failed adding used index: {0}")]
    QueueAddUsed(virtio_queue::Error),
    #[error("Failed creating an iterator over the queue: {0}")]
    QueueIterator(virtio_queue::Error),
    #[error("Guest sent an unexpected balloon statistic tag: {0}")]
    UnexpectedStatTag(u16),
    #[error("Failed to support memory statistics")]
    MemoryStatistic,
}

// Got from include/uapi/linux/virtio_balloon.h
#[repr(C)]
#[derive(Copy, Clone, Debug, Default, Versionize)]
pub struct VirtioBalloonConfig {
    // Number of pages host wants Guest to give up.
    num_pages: u32,
    // Number of pages we've actually got in balloon.
    actual: u32,
    // Free page hinting to speed up migration (this feature is not implemented).
    // Caveat: should not be mixed with free page reporting
    hint_cmd_id: u32,
    // Deflated or reported free pages are initialized with this value (this feature is not implemented).
    poison_val: u32,
    // Number of heterogeneous pages host wants Guest to give up.
    num_hetero_pages: u32,
    // Number of heterogeneous pages we've actually got in balloon.
    hetero_actual: u32,
}

const CONFIG_ACTUAL_OFFSET: u64 = 4;
const CONFIG_HETERO_ACTUAL_OFFSET: u64 = 20;
const CONFIG_ACTUAL_SIZE: usize = 4;

// SAFETY: it only has data and has no implicit padding.
unsafe impl ByteValued for VirtioBalloonConfig {}

struct BalloonEpollHandler {
    mem: GuestMemoryAtomic<GuestMemoryMmap>,
    queues: Vec<Queue>,
    // Fix the mismatch between index into queues and BalloonVq value when some queues are not present due to unsupported features
    queue_indices: HashMap<BalloonVq, usize>,
    interrupt_cb: Arc<dyn VirtioInterrupt>,
    inflate_queue_evt: EventFd,
    deflate_queue_evt: EventFd,
    stats_queue_evt: Option<EventFd>,
    stats_timer_evt: Option<TimerFd>,
    stats_polling_interval: Option<Duration>,
    stats_queue_index: Option<usize>,
    reporting_queue_evt: Option<EventFd>,
    hetero_inflate_queue_evt: Option<EventFd>,
    hetero_deflate_queue_evt: Option<EventFd>,
    kill_evt: EventFd,
    pause_evt: EventFd,
    counters: Arc<BalloonCounters>,
    observe_reporting_returns: bool,
}

impl BalloonEpollHandler {
    // Reporting buffers remain isolated in the Guest until add_used/ACK.
    // Count resident -> absent transitions, rather than advertised bytes:
    // repeat reports of an already punched hole must return zero credit.
    fn resident_bitmap(
        memory: &GuestMemoryMmap,
        base: GuestAddress,
        len: usize,
    ) -> result::Result<Vec<u8>, Error> {
        let page = get_page_size() as usize;
        let region = memory.find_region(base).ok_or(Error::InvalidRequest)?;
        let offset = base.0 - region.start_addr().0;
        if len == 0
            || base.0 % page as u64 != 0
            || len % page != 0
            || offset
                .checked_add(len as u64)
                .ok_or(Error::InvalidRequest)?
                > region.len()
        {
            return Err(Error::InvalidRequest);
        }
        let hva = memory.get_host_address(base).map_err(Error::GuestMemory)?;
        let mut present = vec![0u8; len / page];
        // SAFETY: the complete, page-aligned range belongs to the mapping;
        // mincore only observes and does not fault the range into memory.
        let rc = unsafe { libc::mincore(hva.cast(), len, present.as_mut_ptr()) };
        if rc != 0 {
            return Err(Error::MadviseFail(io::Error::last_os_error()));
        }
        Ok(present)
    }

    fn observed_release(
        memory: &GuestMemoryMmap,
        base: GuestAddress,
        len: usize,
    ) -> result::Result<(u64, u64, u64), Error> {
        let before = Self::resident_bitmap(memory, base, len)?;
        Self::release_memory_range(memory, base, len)?;
        let after = Self::resident_bitmap(memory, base, len)?;
        let page = get_page_size() as u64;
        Ok((
            before.iter().filter(|b| **b & 1 != 0).count() as u64 * page,
            after.iter().filter(|b| **b & 1 != 0).count() as u64 * page,
            before
                .iter()
                .zip(after.iter())
                .filter(|(b, a)| **b & 1 != 0 && **a & 1 == 0)
                .count() as u64
                * page,
        ))
    }

    fn clock_ns(clock: libc::clockid_t) -> u64 {
        let mut t: libc::timespec = unsafe { std::mem::zeroed() };
        // SAFETY: clock_gettime initializes the local timespec.
        if unsafe { libc::clock_gettime(clock, &mut t) } != 0 {
            return 0;
        }
        t.tv_sec as u64 * 1_000_000_000 + t.tv_nsec as u64
    }

    fn signal(&self, int_type: VirtioInterruptType) -> result::Result<(), Error> {
        self.interrupt_cb.trigger(int_type).map_err(|e| {
            error!("Failed to signal used queue: {:?}", e);
            Error::FailedSignal(e)
        })
    }

    fn advise_memory_range(
        memory: &GuestMemoryMmap,
        range_base: GuestAddress,
        range_len: usize,
        advice: libc::c_int,
    ) -> result::Result<(), Error> {
        let hva = memory
            .get_host_address(range_base)
            .map_err(Error::GuestMemory)?;
        let res =
            // SAFETY: Need unsafe to do syscall madvise
            unsafe { libc::madvise(hva as *mut libc::c_void, range_len as libc::size_t, advice) };
        if res != 0 {
            return Err(Error::MadviseFail(io::Error::last_os_error()));
        }
        Ok(())
    }

    fn release_memory_range(
        memory: &GuestMemoryMmap,
        range_base: GuestAddress,
        range_len: usize,
    ) -> result::Result<(), Error> {
        let region = memory.find_region(range_base).ok_or(Error::GuestMemory(
            GuestMemoryError::InvalidGuestAddress(range_base),
        ))?;
        if let Some(f_off) = region.file_offset() {
            let offset = range_base.0 - region.start_addr().0;
            // SAFETY: FFI call with valid arguments
            let res = unsafe {
                libc::fallocate64(
                    f_off.file().as_raw_fd(),
                    libc::FALLOC_FL_PUNCH_HOLE | libc::FALLOC_FL_KEEP_SIZE,
                    (offset + f_off.start()) as libc::off64_t,
                    range_len as libc::off64_t,
                )
            };

            if res != 0 {
                return Err(Error::FallocateFail(io::Error::last_os_error()));
            }
        }

        Self::advise_memory_range(memory, range_base, range_len, libc::MADV_DONTNEED)
    }

    fn process_queue(&mut self, queue: BalloonVq) -> result::Result<(), Error> {
        let queue_index = self.queue_indices[&queue];
        let mut used_descs = false;
        while let Some(mut desc_chain) =
            self.queues[queue_index].pop_descriptor_chain(self.mem.memory())
        {
            let desc = desc_chain.next().ok_or(Error::DescriptorChainTooShort)?;

            let data_chunk_size = size_of::<u32>();

            // The head contains the request type which MUST be readable.
            if desc.is_write_only() {
                error!("The head contains the request type is not right");
                return Err(Error::UnexpectedWriteOnlyDescriptor);
            }
            if desc.len() as usize % data_chunk_size != 0 {
                error!("the request size {} is not right", desc.len());
                return Err(Error::InvalidRequest);
            }

            let mut offset = 0u64;
            while offset < desc.len() as u64 {
                let addr = desc.addr().checked_add(offset).unwrap();
                let pfn: u32 = desc_chain
                    .memory()
                    .read_obj(addr)
                    .map_err(Error::GuestMemory)?;
                offset += data_chunk_size as u64;

                let page_size = get_page_size() as usize;
                let rbase = align_page_size_down((pfn as u64) << VIRTIO_BALLOON_PFN_SHIFT);
                match queue {
                    BalloonVq::Inflate | BalloonVq::HeteroInflate => {
                        Self::release_memory_range(
                            desc_chain.memory(),
                            GuestAddress(rbase),
                            page_size,
                        )?;
                    }
                    BalloonVq::Deflate | BalloonVq::HeteroDeflate => {
                        Self::advise_memory_range(
                            desc_chain.memory(),
                            GuestAddress(rbase),
                            page_size,
                            libc::MADV_WILLNEED,
                        )?;
                    }
                    _ => Err(Error::InvalidQueueIndex(queue_index))?,
                }
            }

            self.queues[queue_index]
                .add_used(desc_chain.memory(), desc_chain.head_index(), desc.len())
                .map_err(Error::QueueAddUsed)?;
            used_descs = true;
        }

        if used_descs {
            self.signal(VirtioInterruptType::Queue(queue_index as u16))
        } else {
            Ok(())
        }
    }

    fn process_stats_timer(&mut self) -> result::Result<(), Error> {
        // This must be set because the driver will send us a buffer after probing
        // `process_stats_queue()` will set the queue_index upon receiving this buffer
        let queue_index = self.stats_queue_index.ok_or(Error::MemoryStatistic)?;

        self.signal(VirtioInterruptType::Queue(queue_index as u16))
    }

    fn process_stats_queue(&mut self, queue: BalloonVq) -> result::Result<(), Error> {
        let queue_index = self.queue_indices[&queue];
        if self.stats_queue_index.is_none() {
            self.stats_queue_index.replace(queue_index);
        }
        let mut used_descs = false;
        while let Some(mut desc_chain) =
            self.queues[queue_index].pop_descriptor_chain(self.mem.memory())
        {
            let desc = desc_chain.next().ok_or(Error::DescriptorChainTooShort)?;

            #[repr(C, packed)]
            #[derive(Copy, Clone, Debug, Default, Versionize)]
            pub struct BalloonStat {
                tag: u16,
                val: u64,
            }
            // SAFETY: BalloonStat is a POB which does not contain any pointers
            unsafe impl ByteValued for BalloonStat {}

            let data_chunk_size = size_of::<BalloonStat>();

            // The head contains the request type which MUST be readable.
            if desc.is_write_only() {
                error!("The head contains the request type is not right");
                return Err(Error::UnexpectedWriteOnlyDescriptor);
            }
            if desc.len() as usize % data_chunk_size != 0 {
                error!("the request size {} is not right", desc.len());
                return Err(Error::InvalidRequest);
            }

            let mut offset = 0u64;
            while offset < desc.len() as u64 {
                let addr = desc.addr().checked_add(offset).unwrap();
                let stat: BalloonStat = desc_chain
                    .memory()
                    .read_obj(addr)
                    .map_err(Error::GuestMemory)?;
                offset += data_chunk_size as u64;
                self.counters
                    .get(stat.tag)?
                    .store(stat.val, Ordering::Relaxed);
            }
            self.queues[queue_index]
                .add_used(desc_chain.memory(), desc_chain.head_index(), desc.len())
                .map_err(Error::QueueAddUsed)?;
            used_descs = true;
        }

        // signal the Guest after the timer goes off to refresh statistics
        if used_descs {
            self.stats_timer_evt
                .as_mut()
                .ok_or(Error::MemoryStatistic)?
                .reset(
                    self.stats_polling_interval.ok_or(Error::MemoryStatistic)?,
                    None,
                )
                .map_err(|_| Error::MemoryStatistic)
        } else {
            Ok(())
        }
    }

    fn process_reporting_queue(&mut self, queue: BalloonVq) -> result::Result<(), Error> {
        let queue_index = self.queue_indices[&queue];
        let mut used_descs = false;
        while let Some(mut desc_chain) =
            self.queues[queue_index].pop_descriptor_chain(self.mem.memory())
        {
            let mut descs_len = 0;
            let mut before_bytes = 0;
            let mut after_bytes = 0;
            let mut returned_bytes = 0;
            let wall = Self::clock_ns(libc::CLOCK_MONOTONIC);
            let cpu = Self::clock_ns(libc::CLOCK_THREAD_CPUTIME_ID);
            while let Some(desc) = desc_chain.next() {
                descs_len += desc.len();
                if self.observe_reporting_returns {
                    let (before, after, returned) = Self::observed_release(
                        desc_chain.memory(),
                        desc.addr(),
                        desc.len() as usize,
                    )?;
                    before_bytes += before;
                    after_bytes += after;
                    returned_bytes += returned;
                } else {
                    Self::release_memory_range(
                        desc_chain.memory(),
                        desc.addr(),
                        desc.len() as usize,
                    )?;
                }
            }

            self.queues[queue_index]
                .add_used(desc_chain.memory(), desc_chain.head_index(), descs_len)
                .map_err(Error::QueueAddUsed)?;
            // Publish only after release succeeded and the chain was ACKed.
            // Cumulative returns are lifecycle events, never a reusable pool:
            // a Guest can refault a reported page immediately after this ACK.
            self.counters.reporting_seq.fetch_add(1, Ordering::AcqRel);
            self.counters
                .reporting_acked_bytes
                .fetch_add(descs_len as u64, Ordering::Relaxed);
            self.counters
                .reporting_before_bytes
                .fetch_add(before_bytes, Ordering::Relaxed);
            self.counters
                .reporting_after_bytes
                .fetch_add(after_bytes, Ordering::Relaxed);
            self.counters
                .reporting_returned_bytes
                .fetch_add(returned_bytes, Ordering::Relaxed);
            self.counters
                .reporting_batches
                .fetch_add(1, Ordering::Relaxed);
            self.counters.reporting_wall_ns.fetch_add(
                Self::clock_ns(libc::CLOCK_MONOTONIC).saturating_sub(wall),
                Ordering::Relaxed,
            );
            self.counters.reporting_cpu_ns.fetch_add(
                Self::clock_ns(libc::CLOCK_THREAD_CPUTIME_ID).saturating_sub(cpu),
                Ordering::Relaxed,
            );
            self.counters.reporting_seq.fetch_add(1, Ordering::Release);
            used_descs = true;
        }

        if used_descs {
            self.signal(VirtioInterruptType::Queue(queue_index as u16))
        } else {
            Ok(())
        }
    }

    fn run(
        &mut self,
        paused: Arc<AtomicBool>,
        paused_sync: Arc<Barrier>,
    ) -> result::Result<(), EpollHelperError> {
        let mut helper = EpollHelper::new(&self.kill_evt, &self.pause_evt)?;
        helper.add_event(self.inflate_queue_evt.as_raw_fd(), INFLATE_QUEUE_EVENT)?;
        helper.add_event(self.deflate_queue_evt.as_raw_fd(), DEFLATE_QUEUE_EVENT)?;
        if let Some(stats_timer_evt) = self.stats_timer_evt.as_ref() {
            helper.add_event(stats_timer_evt.as_raw_fd(), STATS_TIMER_EVENT)?;
        }
        if let Some(stats_queue_evt) = self.stats_queue_evt.as_ref() {
            helper.add_event(stats_queue_evt.as_raw_fd(), STATS_QUEUE_EVENT)?;
        }
        if let Some(reporting_queue_evt) = self.reporting_queue_evt.as_ref() {
            helper.add_event(reporting_queue_evt.as_raw_fd(), REPORTING_QUEUE_EVENT)?;
        }
        if let Some(hetero_inflate_queue_evt) = self.hetero_inflate_queue_evt.as_ref() {
            helper.add_event(
                hetero_inflate_queue_evt.as_raw_fd(),
                HETERO_INFLATE_QUEUE_EVENT,
            )?;
        }
        if let Some(hetero_deflate_queue_evt) = self.hetero_deflate_queue_evt.as_ref() {
            helper.add_event(
                hetero_deflate_queue_evt.as_raw_fd(),
                HETERO_DEFLATE_QUEUE_EVENT,
            )?;
        }

        helper.run(paused, paused_sync, self)?;

        Ok(())
    }
}

impl EpollHelperHandler for BalloonEpollHandler {
    fn handle_event(
        &mut self,
        _helper: &mut EpollHelper,
        event: &epoll::Event,
    ) -> result::Result<(), EpollHelperError> {
        let ev_type = event.data as u16;
        match ev_type {
            INFLATE_QUEUE_EVENT => {
                self.inflate_queue_evt.read().map_err(|e| {
                    EpollHelperError::HandleEvent(anyhow!(
                        "Failed to get inflate queue event: {:?}",
                        e
                    ))
                })?;
                self.process_queue(BalloonVq::Inflate).map_err(|e| {
                    EpollHelperError::HandleEvent(anyhow!(
                        "Failed to signal used inflate queue: {:?}",
                        e
                    ))
                })?;
            }
            DEFLATE_QUEUE_EVENT => {
                self.deflate_queue_evt.read().map_err(|e| {
                    EpollHelperError::HandleEvent(anyhow!(
                        "Failed to get deflate queue event: {:?}",
                        e
                    ))
                })?;
                self.process_queue(BalloonVq::Deflate).map_err(|e| {
                    EpollHelperError::HandleEvent(anyhow!(
                        "Failed to signal used deflate queue: {:?}",
                        e
                    ))
                })?;
            }
            STATS_TIMER_EVENT => {
                if let Some(_stats_timer_evt) = self.stats_timer_evt.as_ref() {
                    self.process_stats_timer().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to signal used statistics queue: {:?}",
                            e
                        ))
                    })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid statistics timer event as no timerfd registered"
                    )));
                }
            }
            STATS_QUEUE_EVENT => {
                if let Some(stats_queue_evt) = self.stats_queue_evt.as_ref() {
                    stats_queue_evt.read().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to get statistics queue event: {:?}",
                            e
                        ))
                    })?;
                    self.process_stats_queue(BalloonVq::Stats).map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to consume available memory statistics: {:?}",
                            e
                        ))
                    })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid statistics queue event as no eventfd registered"
                    )));
                }
            }
            REPORTING_QUEUE_EVENT => {
                if let Some(reporting_queue_evt) = self.reporting_queue_evt.as_ref() {
                    reporting_queue_evt.read().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to get reporting queue event: {:?}",
                            e
                        ))
                    })?;
                    self.process_reporting_queue(BalloonVq::Reporting)
                        .map_err(|e| {
                            EpollHelperError::HandleEvent(anyhow!(
                                "Failed to signal used reporting queue: {:?}",
                                e
                            ))
                        })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid reporting queue event as no eventfd registered"
                    )));
                }
            }
            HETERO_INFLATE_QUEUE_EVENT => {
                if let Some(hetero_inflate_queue_evt) = self.hetero_inflate_queue_evt.as_ref() {
                    hetero_inflate_queue_evt.read().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to get heterogeneous inflate queue event: {:?}",
                            e
                        ))
                    })?;
                    self.process_queue(BalloonVq::HeteroInflate).map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to signal used heterogeneous inflate queue: {:?}",
                            e
                        ))
                    })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid heterogeneous inflate queue event as no eventfd registered"
                    )));
                }
            }
            HETERO_DEFLATE_QUEUE_EVENT => {
                if let Some(hetero_deflate_queue_evt) = self.hetero_deflate_queue_evt.as_ref() {
                    hetero_deflate_queue_evt.read().map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to get heterogeneous deflate queue event: {:?}",
                            e
                        ))
                    })?;
                    self.process_queue(BalloonVq::HeteroDeflate).map_err(|e| {
                        EpollHelperError::HandleEvent(anyhow!(
                            "Failed to signal used heterogeneous deflate queue: {:?}",
                            e
                        ))
                    })?;
                } else {
                    return Err(EpollHelperError::HandleEvent(anyhow!(
                        "Invalid heterogeneous deflate queue event as no eventfd registered"
                    )));
                }
            }
            _ => {
                return Err(EpollHelperError::HandleEvent(anyhow!(
                    "Unknown event for virtio-balloon"
                )));
            }
        }

        Ok(())
    }
}

#[derive(Versionize)]
pub struct BalloonState {
    pub avail_features: u64,
    pub acked_features: u64,
    pub config: VirtioBalloonConfig,
}

impl VersionMapped for BalloonState {}

// Virtio device for exposing entropy to the guest OS through virtio.
pub struct Balloon {
    common: VirtioCommon,
    id: String,
    config: VirtioBalloonConfig,
    seccomp_action: SeccompAction,
    exit_evt: EventFd,
    interrupt_cb: Option<Arc<dyn VirtioInterrupt>>,
    counters: Arc<BalloonCounters>,
    stats_polling_interval: Option<Duration>,
}

impl Balloon {
    // Create a new virtio-balloon.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        id: String,
        size: [u64; 2],
        stats_polling_interval: Option<Duration>,
        deflate_on_oom: bool,
        free_page_reporting: bool,
        heterogeneous_memory: bool,
        seccomp_action: SeccompAction,
        exit_evt: EventFd,
        state: Option<BalloonState>,
    ) -> io::Result<Self> {
        let mut queue_sizes = vec![QUEUE_SIZE; MIN_NUM_QUEUES];

        let (avail_features, acked_features, config, paused) = if let Some(state) = state {
            info!("Restoring virtio-balloon {}", id);
            (
                state.avail_features,
                state.acked_features,
                state.config,
                true,
            )
        } else {
            let mut avail_features = 1u64 << VIRTIO_F_VERSION_1;
            if stats_polling_interval.is_some() {
                avail_features |= 1u64 << VIRTIO_BALLOON_F_STATS_VQ;
            }
            if deflate_on_oom {
                avail_features |= 1u64 << VIRTIO_BALLOON_F_DEFLATE_ON_OOM;
            }
            if free_page_reporting {
                avail_features |= 1u64 << VIRTIO_BALLOON_F_REPORTING;
            }
            if heterogeneous_memory {
                avail_features |= 1u64 << VIRTIO_BALLOON_F_HETERO_MEM;
            }

            let config = VirtioBalloonConfig {
                num_pages: (size[0] >> VIRTIO_BALLOON_PFN_SHIFT) as u32,
                num_hetero_pages: (size[1] >> VIRTIO_BALLOON_PFN_SHIFT) as u32,
                ..Default::default()
            };

            (avail_features, 0, config, false)
        };

        if stats_polling_interval.is_some() {
            queue_sizes.push(STATS_QUEUE_SIZE);
        }
        if free_page_reporting {
            queue_sizes.push(REPORTING_QUEUE_SIZE);
        }
        if heterogeneous_memory {
            queue_sizes.extend_from_slice(&[QUEUE_SIZE; 2]);
        }

        Ok(Balloon {
            common: VirtioCommon {
                device_type: VirtioDeviceType::Balloon as u32,
                avail_features,
                acked_features,
                paused_sync: Some(Arc::new(Barrier::new(2))),
                queue_sizes,
                min_queues: MIN_NUM_QUEUES as u16,
                paused: Arc::new(AtomicBool::new(paused)),
                ..Default::default()
            },
            id,
            config,
            seccomp_action,
            exit_evt,
            interrupt_cb: None,
            counters: Arc::new(BalloonCounters::default()),
            stats_polling_interval,
        })
    }

    pub fn resize(&mut self, size: [u64; 2]) -> Result<(), Error> {
        self.config.num_pages = (size[0] >> VIRTIO_BALLOON_PFN_SHIFT) as u32;
        self.config.num_hetero_pages = (size[1] >> VIRTIO_BALLOON_PFN_SHIFT) as u32;

        if let Some(interrupt_cb) = &self.interrupt_cb {
            interrupt_cb
                .trigger(VirtioInterruptType::Config)
                .map_err(Error::FailedSignal)
        } else {
            Ok(())
        }
    }

    // Get the actual size of the virtio-balloon.
    pub fn get_actual(&self) -> u64 {
        (self.config.actual as u64) << VIRTIO_BALLOON_PFN_SHIFT
    }

    // Get the actual size of the virtio-balloon.
    pub fn get_hetero_actual(&self) -> u64 {
        (self.config.hetero_actual as u64) << VIRTIO_BALLOON_PFN_SHIFT
    }

    fn state(&self) -> BalloonState {
        BalloonState {
            avail_features: self.common.avail_features,
            acked_features: self.common.acked_features,
            config: self.config,
        }
    }

    #[cfg(fuzzing)]
    pub fn wait_for_epoll_threads(&mut self) {
        self.common.wait_for_epoll_threads();
    }
}

impl Drop for Balloon {
    fn drop(&mut self) {
        if let Some(kill_evt) = self.common.kill_evt.take() {
            // Ignore the result because there is nothing we can do about it.
            let _ = kill_evt.write(1);
        }
        self.common.wait_for_epoll_threads();
    }
}

impl VirtioDevice for Balloon {
    fn device_type(&self) -> u32 {
        self.common.device_type
    }

    fn queue_max_sizes(&self) -> &[u16] {
        &self.common.queue_sizes
    }

    fn features(&self) -> u64 {
        self.common.avail_features
    }

    fn ack_features(&mut self, value: u64) {
        self.common.ack_features(value)
    }

    fn read_config(&self, offset: u64, data: &mut [u8]) {
        self.read_config_from_slice(self.config.as_slice(), offset, data);
    }

    fn write_config(&mut self, offset: u64, data: &[u8]) {
        // The "actual" and "hetero_actual" fields are the only mutable fields
        if (offset != CONFIG_ACTUAL_OFFSET && offset != CONFIG_HETERO_ACTUAL_OFFSET)
            || data.len() != CONFIG_ACTUAL_SIZE
        {
            error!(
                "Attempt to write to read-only field: offset {:x} length {}",
                offset,
                data.len()
            );
            return;
        }

        let config = self.config.as_mut_slice();
        let config_len = config.len() as u64;
        let data_len = data.len() as u64;
        if offset + data_len > config_len {
            error!(
                    "Out-of-bound access to configuration: config_len = {} offset = {:x} length = {} for {}",
                    config_len,
                    offset,
                    data_len,
                    self.device_type()
                );
            return;
        }

        if let Some(end) = offset.checked_add(config.len() as u64) {
            let mut offset_config =
                &mut config[offset as usize..std::cmp::min(end, config_len) as usize];
            offset_config.write_all(data).unwrap();
        }
    }

    fn activate(
        &mut self,
        mem: GuestMemoryAtomic<GuestMemoryMmap>,
        interrupt_cb: Arc<dyn VirtioInterrupt>,
        mut queues: Vec<(usize, Queue, EventFd)>,
    ) -> ActivateResult {
        self.common.activate(&queues, &interrupt_cb)?;
        let (kill_evt, pause_evt) = self.common.dup_eventfds();

        let mut virtqueues = Vec::new();
        let mut queue_indices = HashMap::new();
        let (_, queue, queue_evt) = queues.remove(0);
        queue_indices.insert(BalloonVq::Inflate, virtqueues.len());
        virtqueues.push(queue);
        let inflate_queue_evt = queue_evt;
        let (_, queue, queue_evt) = queues.remove(0);
        queue_indices.insert(BalloonVq::Deflate, virtqueues.len());
        virtqueues.push(queue);
        let deflate_queue_evt = queue_evt;
        let (stats_queue_evt, stats_timer_evt) =
            if self.common.feature_acked(VIRTIO_BALLOON_F_STATS_VQ) && !queues.is_empty() {
                let (_, queue, queue_evt) = queues.remove(0);
                queue_indices.insert(BalloonVq::Stats, virtqueues.len());
                virtqueues.push(queue);
                let timer_evt = TimerFd::new().map_err(|_| ActivateError::BadActivate)?;
                (Some(queue_evt), Some(timer_evt))
            } else {
                (None, None)
            };
        let reporting_queue_evt =
            if self.common.feature_acked(VIRTIO_BALLOON_F_REPORTING) && !queues.is_empty() {
                let (_, queue, queue_evt) = queues.remove(0);
                queue_indices.insert(BalloonVq::Reporting, virtqueues.len());
                virtqueues.push(queue);
                Some(queue_evt)
            } else {
                None
            };
        let hetero_inflate_queue_evt =
            if self.common.feature_acked(VIRTIO_BALLOON_F_HETERO_MEM) && !queues.is_empty() {
                let (_, queue, queue_evt) = queues.remove(0);
                queue_indices.insert(BalloonVq::HeteroInflate, virtqueues.len());
                virtqueues.push(queue);
                Some(queue_evt)
            } else {
                None
            };
        let hetero_deflate_queue_evt =
            if self.common.feature_acked(VIRTIO_BALLOON_F_HETERO_MEM) && !queues.is_empty() {
                let (_, queue, queue_evt) = queues.remove(0);
                queue_indices.insert(BalloonVq::HeteroDeflate, virtqueues.len());
                virtqueues.push(queue);
                Some(queue_evt)
            } else {
                None
            };

        self.interrupt_cb = Some(interrupt_cb.clone());

        if get_page_size() != 1 << VIRTIO_BALLOON_PFN_SHIFT {
            // Do not support mismatched page size
            return Err(ActivateError::BadActivate);
        }

        let mut handler = BalloonEpollHandler {
            mem,
            queues: virtqueues,
            queue_indices,
            interrupt_cb,
            inflate_queue_evt,
            deflate_queue_evt,
            stats_queue_evt,
            stats_timer_evt,
            stats_polling_interval: self.stats_polling_interval,
            stats_queue_index: None,
            reporting_queue_evt,
            hetero_inflate_queue_evt,
            hetero_deflate_queue_evt,
            kill_evt,
            pause_evt,
            counters: self.counters.clone(),
            observe_reporting_returns: std::env::var("DEMETER_REPORTING_RECEIPTS").as_deref()
                == Ok("1"),
        };
        self.counters
            .reporting_observed
            .store(handler.observe_reporting_returns as u64, Ordering::Relaxed);

        let paused = self.common.paused.clone();
        let paused_sync = self.common.paused_sync.clone();
        let mut epoll_threads = Vec::new();

        spawn_virtio_thread(
            &self.id,
            &self.seccomp_action,
            Thread::VirtioBalloon,
            &mut epoll_threads,
            &self.exit_evt,
            move || handler.run(paused, paused_sync.unwrap()),
        )?;
        self.common.epoll_threads = Some(epoll_threads);

        event!("virtio-device", "activated", "id", &self.id);
        Ok(())
    }

    fn reset(&mut self) -> Option<Arc<dyn VirtioInterrupt>> {
        let result = self.common.reset();
        event!("virtio-device", "reset", "id", &self.id);
        result
    }

    fn counters(&self) -> Option<HashMap<&'static str, Wrapping<u64>>> {
        let mut map: HashMap<_, _> = (0..16)
            .map(|i| {
                (
                    // SAFETY: the maximum tag number is 11
                    self.counters.name(i).unwrap(),
                    Wrapping(self.counters.get(i).unwrap().load(Ordering::Relaxed)),
                )
            })
            .collect();
        map.insert("actual", Wrapping(self.get_actual()));
        map.insert("hetero_actual", Wrapping(self.get_hetero_actual()));
        loop {
            let seq = self.counters.reporting_seq.load(Ordering::Acquire);
            if seq & 1 != 0 {
                std::hint::spin_loop();
                continue;
            }
            for (name, counter) in [
                ("reporting_observed", &self.counters.reporting_observed),
                (
                    "reporting_acked_bytes",
                    &self.counters.reporting_acked_bytes,
                ),
                (
                    "reporting_before_bytes",
                    &self.counters.reporting_before_bytes,
                ),
                (
                    "reporting_after_bytes",
                    &self.counters.reporting_after_bytes,
                ),
                (
                    "reporting_returned_bytes",
                    &self.counters.reporting_returned_bytes,
                ),
                ("reporting_batches", &self.counters.reporting_batches),
                ("reporting_wall_ns", &self.counters.reporting_wall_ns),
                ("reporting_cpu_ns", &self.counters.reporting_cpu_ns),
            ] {
                map.insert(name, Wrapping(counter.load(Ordering::Relaxed)));
            }
            std::sync::atomic::fence(Ordering::Acquire);
            if self.counters.reporting_seq.load(Ordering::Relaxed) == seq {
                break;
            }
        }
        Some(map)
    }
}

impl Pausable for Balloon {
    fn pause(&mut self) -> result::Result<(), MigratableError> {
        self.common.pause()
    }

    fn resume(&mut self) -> result::Result<(), MigratableError> {
        self.common.resume()
    }
}

impl Snapshottable for Balloon {
    fn id(&self) -> String {
        self.id.clone()
    }

    fn snapshot(&mut self) -> std::result::Result<Snapshot, MigratableError> {
        Snapshot::new_from_versioned_state(&self.state())
    }
}
impl Transportable for Balloon {}
impl Migratable for Balloon {}

#[derive(Debug, Default)]
pub struct BalloonCounters {
    reporting_seq: AtomicU64,
    reporting_observed: AtomicU64,
    reporting_acked_bytes: AtomicU64,
    reporting_before_bytes: AtomicU64,
    reporting_after_bytes: AtomicU64,
    reporting_returned_bytes: AtomicU64,
    reporting_batches: AtomicU64,
    reporting_wall_ns: AtomicU64,
    reporting_cpu_ns: AtomicU64,
    swap_in: AtomicU64,
    swap_out: AtomicU64,
    major_faults: AtomicU64,
    minor_faults: AtomicU64,
    free_memory: AtomicU64,
    total_memory: AtomicU64,
    available_memory: AtomicU64,
    disk_caches: AtomicU64,
    hugetlb_allocations: AtomicU64,
    hugetlb_failures: AtomicU64,
    dram_accesses: AtomicU64,
    dram_free: AtomicU64,
    dram_total: AtomicU64,
    pmem_accesses: AtomicU64,
    pmem_free: AtomicU64,
    pmem_total: AtomicU64,
}

impl Index<u16> for BalloonCounters {
    type Output = AtomicU64;

    fn index(&self, index: u16) -> &Self::Output {
        self.get(index).expect("unexpected memory statistic tag")
    }
}

impl BalloonCounters {
    fn get(&self, tag: u16) -> result::Result<&AtomicU64, Error> {
        Ok(match tag {
            0 => &self.swap_in,
            1 => &self.swap_out,
            2 => &self.major_faults,
            3 => &self.minor_faults,
            4 => &self.free_memory,
            5 => &self.total_memory,
            6 => &self.available_memory,
            7 => &self.disk_caches,
            8 => &self.hugetlb_allocations,
            9 => &self.hugetlb_failures,
            10 => &self.dram_accesses,
            11 => &self.dram_free,
            12 => &self.dram_total,
            13 => &self.pmem_accesses,
            14 => &self.pmem_free,
            15 => &self.pmem_total,
            _ => return Err(Error::UnexpectedStatTag(tag)),
        })
    }
    fn name(&self, tag: u16) -> result::Result<&'static str, Error> {
        Ok(match tag {
            0 => "swap_in",
            1 => "swap_out",
            2 => "major_faults",
            3 => "minor_faults",
            4 => "free_memory",
            5 => "total_memory",
            6 => "available_memory",
            7 => "disk_caches",
            8 => "hugetlb_allocations",
            9 => "hugetlb_failures",
            10 => "dram_accesses",
            11 => "dram_free",
            12 => "dram_total",
            13 => "pmem_accesses",
            14 => "pmem_free",
            15 => "pmem_total",
            _ => return Err(Error::UnexpectedStatTag(tag)),
        })
    }
}

#[cfg(test)]
mod reporting_receipt_tests {
    use super::*;
    use std::fs::File;
    use std::os::unix::io::FromRawFd;
    use vm_memory::FileOffset;

    fn shared_memory(pages: usize) -> GuestMemoryMmap {
        // SAFETY: static terminated name and checked descriptor ownership.
        let fd =
            unsafe { libc::memfd_create(b"receipt-test\0".as_ptr().cast(), libc::MFD_CLOEXEC) };
        assert!(fd >= 0);
        let file = unsafe { File::from_raw_fd(fd) };
        let len = pages * get_page_size() as usize;
        file.set_len(len as u64).unwrap();
        GuestMemoryMmap::from_ranges_with_files(&[(
            GuestAddress(0),
            len,
            Some(FileOffset::new(file, 0)),
        )])
        .unwrap()
    }

    #[test]
    fn reporting_receipt_counts_resident_returns_and_repeat_holes() {
        let memory = shared_memory(16);
        let page = get_page_size() as usize;
        for i in [0, 2, 5, 8, 11, 12, 15] {
            memory
                .write_obj(42u8, GuestAddress((i * page) as u64))
                .unwrap();
        }
        assert_eq!(
            BalloonEpollHandler::observed_release(&memory, GuestAddress(0), 16 * page).unwrap(),
            (7 * page as u64, 0, 7 * page as u64)
        );
        assert_eq!(
            BalloonEpollHandler::observed_release(&memory, GuestAddress(0), 16 * page).unwrap(),
            (0, 0, 0)
        );
        // Refaults create a fresh lifecycle; returned bytes are not a reserve.
        for i in [1, 2, 9] {
            memory
                .write_obj(43u8, GuestAddress((i * page) as u64))
                .unwrap();
        }
        assert_eq!(
            BalloonEpollHandler::observed_release(&memory, GuestAddress(0), 16 * page).unwrap(),
            (3 * page as u64, 0, 3 * page as u64)
        );
    }

    #[test]
    fn reporting_receipt_rejects_unaligned_or_cross_region_ranges() {
        let memory = shared_memory(2);
        let page = get_page_size() as usize;
        memory.write_obj(41u8, GuestAddress(0)).unwrap();
        for (base, len) in [(0, 0), (1, page), (0, page + 1), (page, 2 * page)] {
            assert!(
                BalloonEpollHandler::observed_release(&memory, GuestAddress(base as u64), len)
                    .is_err()
            );
        }
        assert_eq!(memory.read_obj::<u8>(GuestAddress(0)).unwrap(), 41);
    }
}
