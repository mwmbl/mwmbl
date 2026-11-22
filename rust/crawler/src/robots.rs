use std::{
    cmp::min,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use anyhow::bail;
use priority_queue::DoublePriorityQueue;
use reqwest::{Client, StatusCode, Url};
use scc::HashMap;
use texting_robots::Robot;
use tokio::{sync::RwLock, time::Instant};

use crate::{
    constants::{
        BLOCK_COOL_OFF, DEFAULT_DOMAIN_DELAY, MAX_BLOCK_COOL_OFF, ROBOTS_CACHE_ERROR_TTL_SECONDS,
        ROBOTS_CACHE_TTL_SECONDS, ROBOTS_USER_AGENT,
    },
    retrieve::fetch,
};

#[derive(Clone)]
pub struct CachedRobotFile {
    cache_ttl: Instant,
    robot: Option<Arc<Robot>>,
    error_count: Arc<AtomicU64>,
}

impl CachedRobotFile {
    pub fn new(cache_ttl: Duration, txt: Option<&[u8]>) -> Self {
        let robot = if let Some(txt) = txt
            && let Ok(robot) = Robot::new(ROBOTS_USER_AGENT, txt)
        {
            Some(Arc::new(robot))
        } else {
            None
        };
        Self {
            cache_ttl: Instant::now() + cache_ttl,
            robot,
            error_count: Arc::new(0.into()),
        }
    }
    pub fn can_fetch(&self, url: &Url) -> Option<bool> {
        if let Some(robot) = &self.robot {
            return Some(robot.allowed(url.as_str()));
        }
        None
    }
    pub fn delay(&self, status: Option<StatusCode>) -> Duration {
        if status.is_some_and(|status| !status.is_success()) {
            let value = self.error_count.fetch_add(1, Ordering::Relaxed);
            return min(BLOCK_COOL_OFF * (1 << value.min(31)), MAX_BLOCK_COOL_OFF);
        }
        self.error_count.store(0, Ordering::Relaxed);

        if let Some(robot) = &self.robot {
            return robot
                .delay
                .map_or(DEFAULT_DOMAIN_DELAY, Duration::from_secs_f32);
        }
        ROBOTS_CACHE_ERROR_TTL_SECONDS
    }
    pub fn is_valid(&self) -> bool {
        self.cache_ttl > Instant::now()
    }
}

fn get_robot_url(url: &Url) -> Url {
    let mut robot_path = url.clone();
    robot_path.set_path("robots.txt");
    robot_path.set_query(None);
    robot_path.set_fragment(None);
    robot_path
}

#[derive(Clone)]
pub struct RobotCache {
    cache: Arc<HashMap<String, CachedRobotFile>>,
    removal: Arc<RwLock<DoublePriorityQueue<String, Instant>>>,
}

impl RobotCache {
    #[must_use]
    pub fn new() -> Self {
        RobotCache {
            cache: Arc::new(HashMap::default()),
            removal: Arc::new(RwLock::new(DoublePriorityQueue::new())),
        }
    }

    async fn evict(&self) {
        let mut should_try = false;
        // don't ask for write lock if not necessary
        let lock = self.removal.read().await;
        if let Some((_, ttl)) = lock.peek_min() {
            should_try = ttl <= &mut Instant::now();
        }
        drop(lock);
        if !should_try {
            return;
        }
        let mut lock = self.removal.write().await;
        let mut to_remove = Vec::new();
        while let Some((key, _)) = lock.pop_min_if(|_, ttl| ttl <= &mut Instant::now()) {
            to_remove.push(key);
        }
        drop(lock);
        for key in to_remove {
            self.cache.remove_if_async(&key, |v| !v.is_valid()).await;
        }
    }

    pub async fn can_fetch(&self, domain: &str, url: &Url) -> Option<bool> {
        self.evict().await;
        if let Some(occupied_entry) = self.cache.get_async(domain).await {
            return if occupied_entry.get().is_valid() {
                occupied_entry.get().can_fetch(url)
            } else {
                let _ = occupied_entry.remove();
                None
            };
        }
        None
    }

    pub async fn domain_delay(&self, status: Option<StatusCode>, domain: &str) -> Duration {
        self.evict().await;
        if let Some(occupied_entry) = self.cache.get_async(domain).await {
            let delay = occupied_entry.delay(status);
            if !occupied_entry.get().is_valid() {
                let _ = occupied_entry.remove();
            }
            return delay;
        }
        ROBOTS_CACHE_ERROR_TTL_SECONDS
    }

    pub async fn put(&mut self, domain: &str, file: CachedRobotFile) {
        self.evict().await;
        let mut lock = self.removal.write().await;
        lock.push(domain.to_owned(), file.cache_ttl);
        drop(lock);
        let _ = self.cache.insert_async(domain.to_owned(), file).await;
    }
}

pub async fn robot_allowed(
    url: &Url,
    domain: &str,
    client: &Client,
    robots_cache: &mut RobotCache,
) -> anyhow::Result<bool> {
    let robot_url = get_robot_url(url);

    if let Some(can_fetch) = robots_cache.can_fetch(domain, url).await {
        return Ok(can_fetch);
    }

    let v = fetch(client, &robot_url).await;

    if let Ok((data, status, _)) = v
        && (status.is_success() || (status.is_client_error() && status.as_u16() != 429))
    {
        let robot =  if status.is_client_error() && status.as_u16() != 429 {
            CachedRobotFile::new(ROBOTS_CACHE_TTL_SECONDS, Some(&[])) // default to allow all if robots is 4xx
        } else {
            CachedRobotFile::new(ROBOTS_CACHE_TTL_SECONDS, Some(&data))
        };
        let allowed = robot.can_fetch(url).unwrap_or_default();
        robots_cache.put(domain, robot).await;
        return Ok(allowed);
    }
    robots_cache
        .put(
            domain,
            CachedRobotFile::new(ROBOTS_CACHE_ERROR_TTL_SECONDS, None),
        )
        .await;
    bail!("robots.txt unavailable for {domain}");
}
