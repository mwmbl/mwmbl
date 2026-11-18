//! Memory analysis instrumentation. Counters are always compiled (they are plain atomics);
//! the reporter and jemalloc stats only run with the `memprof` feature.

use std::sync::atomic::{AtomicI64, Ordering};

/// Tasks spawned by `Crawler::run` that are still alive (from spawn to end of task).
pub static CRAWL_TASKS: AtomicI64 = AtomicI64::new(0);
/// Of those, how many are still inside `get_url`.
pub static IN_GET_URL: AtomicI64 = AtomicI64::new(0);
/// Of those, how many are inside `crawl` (fetch + parse).
pub static IN_CRAWL: AtomicI64 = AtomicI64::new(0);
/// Bytes of response bodies currently buffered in `fetch`.
pub static FETCH_BYTES: AtomicI64 = AtomicI64::new(0);
/// `release_after_delay` tasks still sleeping, holding a domain claim.
pub static RELEASE_PENDING: AtomicI64 = AtomicI64::new(0);
/// `add_domain_urls_later` tasks still waiting for their domain lock.
pub static ADD_URLS_PENDING: AtomicI64 = AtomicI64::new(0);
/// `FoundURL`s held by those waiting tasks.
pub static ADD_URLS_PENDING_URLS: AtomicI64 = AtomicI64::new(0);
/// Robots.txt fetches started.
pub static ROBOTS_FETCHES: AtomicI64 = AtomicI64::new(0);
/// Page fetches started.
pub static PAGE_FETCHES: AtomicI64 = AtomicI64::new(0);

pub fn inc(c: &AtomicI64, n: i64) {
    c.fetch_add(n, Ordering::Relaxed);
}

pub fn dec(c: &AtomicI64, n: i64) {
    c.fetch_sub(n, Ordering::Relaxed);
}

pub fn get(c: &AtomicI64) -> i64 {
    c.load(Ordering::Relaxed)
}

/// Decrements a counter when dropped, so cancelled/early-returning paths are counted too.
pub struct Gauge(&'static AtomicI64, i64);

impl Gauge {
    pub fn new(c: &'static AtomicI64, n: i64) -> Self {
        inc(c, n);
        Gauge(c, n)
    }
}

impl Drop for Gauge {
    fn drop(&mut self) {
        dec(self.0, self.1);
    }
}

pub fn rss_bytes() -> u64 {
    std::fs::read_to_string("/proc/self/statm")
        .ok()
        .and_then(|s| s.split_whitespace().nth(1).and_then(|v| v.parse::<u64>().ok()))
        .map_or(0, |pages| pages * 4096)
}

#[cfg(feature = "memprof")]
pub mod jemalloc {
    use tikv_jemalloc_ctl::{epoch, stats};

    pub fn allocated_resident() -> (usize, usize) {
        let _ = epoch::advance();
        (
            stats::allocated::read().unwrap_or(0),
            stats::resident::read().unwrap_or(0),
        )
    }

    /// Writes a heap profile (needs `MALLOC_CONF=prof:true`); analyse with jeprof.
    pub fn dump_profile(path: &str) {
        let mut bytes = path.as_bytes().to_vec();
        bytes.push(0);
        // SAFETY: prof.dump takes a NUL-terminated path.
        let res = unsafe { tikv_jemalloc_ctl::raw::write(b"prof.dump\0", bytes.as_ptr()) };
        if let Err(e) = res {
            eprintln!("[memstats] prof.dump failed: {e}");
        }
    }
}

impl Gauge {
    /// Grows a gauge by `n` (the caller has already incremented the counter).
    pub fn adopt(mut g: Gauge, n: i64) -> Gauge {
        g.1 += n;
        g
    }
}
