// Copyright © 2021 Intel Corporation
//
// SPDX-License-Identifier: Apache-2.0 AND BSD-3-Clause

use crate::async_io::{
    AsyncIo, AsyncIoError, AsyncIoResult, DiskFile, DiskFileError, DiskFileResult,
};
use crate::DiskTopology;
use io_uring::{opcode, squeue, types, IoUring};
use std::fs::File;
use std::io::{Seek, SeekFrom};
use std::os::unix::io::{AsRawFd, RawFd};
use vmm_sys_util::eventfd::EventFd;

pub struct RawFileDisk {
    file: File,
}

impl RawFileDisk {
    pub fn new(file: File) -> Self {
        RawFileDisk { file }
    }
}

impl DiskFile for RawFileDisk {
    fn size(&mut self) -> DiskFileResult<u64> {
        self.file
            .seek(SeekFrom::End(0))
            .map_err(DiskFileError::Size)
    }

    fn new_async_io(&self, ring_depth: u32) -> DiskFileResult<Box<dyn AsyncIo>> {
        Ok(Box::new(
            RawFileAsync::new(self.file.as_raw_fd(), ring_depth)
                .map_err(DiskFileError::NewAsyncIo)?,
        ) as Box<dyn AsyncIo>)
    }

    fn topology(&mut self) -> DiskTopology {
        if let Ok(topology) = DiskTopology::probe(&self.file) {
            topology
        } else {
            warn!("Unable to get device topology. Using default topology");
            DiskTopology::default()
        }
    }
}

pub struct RawFileAsync {
    fd: RawFd,
    io_uring: IoUring,
    eventfd: EventFd,
}

impl RawFileAsync {
    pub fn new(fd: RawFd, ring_depth: u32) -> std::io::Result<Self> {
        let io_uring = IoUring::new(ring_depth)?;
        // Buffered image/swap I/O may block on host reclaim. The kernel's
        // unbounded pool otherwise scales with RLIMIT_NPROC, allowing one VMM
        // to create thousands of workers under memory pressure. Bound both
        // pools before the first submission; keep asynchronous I/O and seccomp.
        let mut previous_worker_limits = [8, 8];
        io_uring
            .submitter()
            .register_iowq_max_workers(&mut previous_worker_limits)?;
        let eventfd = EventFd::new(libc::EFD_NONBLOCK)?;

        // Register the io_uring eventfd that will notify when something in
        // the completion queue is ready.
        io_uring.submitter().register_eventfd(eventfd.as_raw_fd())?;

        Ok(RawFileAsync {
            fd,
            io_uring,
            eventfd,
        })
    }
}

impl AsyncIo for RawFileAsync {
    fn notifier(&self) -> &EventFd {
        &self.eventfd
    }

    fn read_vectored(
        &mut self,
        offset: libc::off_t,
        iovecs: &[libc::iovec],
        user_data: u64,
    ) -> AsyncIoResult<()> {
        let (submitter, mut sq, _) = self.io_uring.split();

        // SAFETY: we know the file descriptor is valid and we
        // relied on vm-memory to provide the buffer address.
        let _ = unsafe {
            sq.push(
                &opcode::Readv::new(types::Fd(self.fd), iovecs.as_ptr(), iovecs.len() as u32)
                    .offset(offset.try_into().unwrap())
                    .build()
                    .flags(squeue::Flags::ASYNC)
                    .user_data(user_data),
            )
        };

        // Update the submission queue and submit new operations to the
        // io_uring instance.
        sq.sync();
        submitter.submit().map_err(AsyncIoError::ReadVectored)?;

        Ok(())
    }

    fn write_vectored(
        &mut self,
        offset: libc::off_t,
        iovecs: &[libc::iovec],
        user_data: u64,
    ) -> AsyncIoResult<()> {
        let (submitter, mut sq, _) = self.io_uring.split();

        // SAFETY: we know the file descriptor is valid and we
        // relied on vm-memory to provide the buffer address.
        let _ = unsafe {
            sq.push(
                &opcode::Writev::new(types::Fd(self.fd), iovecs.as_ptr(), iovecs.len() as u32)
                    .offset(offset.try_into().unwrap())
                    .build()
                    .flags(squeue::Flags::ASYNC)
                    .user_data(user_data),
            )
        };

        // Update the submission queue and submit new operations to the
        // io_uring instance.
        sq.sync();
        submitter.submit().map_err(AsyncIoError::WriteVectored)?;

        Ok(())
    }

    fn fsync(&mut self, user_data: Option<u64>) -> AsyncIoResult<()> {
        if let Some(user_data) = user_data {
            let (submitter, mut sq, _) = self.io_uring.split();

            // SAFETY: we know the file descriptor is valid.
            let _ = unsafe {
                sq.push(
                    &opcode::Fsync::new(types::Fd(self.fd))
                        .build()
                        .flags(squeue::Flags::ASYNC)
                        .user_data(user_data),
                )
            };

            // Update the submission queue and submit new operations to the
            // io_uring instance.
            sq.sync();
            submitter.submit().map_err(AsyncIoError::Fsync)?;
        } else {
            // SAFETY: FFI call with a valid fd
            unsafe { libc::fsync(self.fd) };
        }

        Ok(())
    }

    fn next_completed_request(&mut self) -> Option<(u64, i32)> {
        self.io_uring
            .completion()
            .next()
            .map(|entry| (entry.user_data(), entry.result()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::OpenOptions;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn capped_workers_preserve_async_write_read_and_fsync() {
        let path = std::env::temp_dir().join(format!(
            "PhaseTieing-iowq-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        std::fs::remove_file(&path).unwrap();
        let mut io = RawFileAsync::new(file.as_raw_fd(), 8).unwrap();
        let mut written = vec![0x5au8; 4096];
        let input = [libc::iovec {
            iov_base: written.as_mut_ptr().cast(),
            iov_len: written.len(),
        }];
        io.write_vectored(0, &input, 1).unwrap();
        io.io_uring.submitter().submit_and_wait(1).unwrap();
        assert_eq!(io.next_completed_request(), Some((1, 4096)));
        io.fsync(Some(2)).unwrap();
        io.io_uring.submitter().submit_and_wait(1).unwrap();
        assert_eq!(io.next_completed_request(), Some((2, 0)));
        let mut read = vec![0u8; 4096];
        let output = [libc::iovec {
            iov_base: read.as_mut_ptr().cast(),
            iov_len: read.len(),
        }];
        io.read_vectored(0, &output, 3).unwrap();
        io.io_uring.submitter().submit_and_wait(1).unwrap();
        assert_eq!(io.next_completed_request(), Some((3, 4096)));
        assert_eq!(read, written);
        // After actual ASYNC work, zero requests query the existing pool limits.
        let mut limits = [0, 0];
        io.io_uring
            .submitter()
            .register_iowq_max_workers(&mut limits)
            .unwrap();
        assert_eq!(limits, [8, 8]);
    }
}
