use std::{
    cmp::min,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use anyhow::bail;
use reqwest::{Client, StatusCode, Url};
use scc::HashCache;
use texting_robots::Robot;
use tokio::time::Instant;

use crate::{
    constants::{
        BLOCK_COOL_OFF, DEFAULT_DOMAIN_DELAY, MAX_BLOCK_COOL_OFF, ROBOTS_CACHE_ERROR_TTL_SECONDS,
        ROBOTS_CACHE_TTL_SECONDS, ROBOTS_USER_AGENT,
    },
    retrieve::fetch,
};

#[derive(Clone)]
pub struct CachedRobotFile {
    cache_ttl: Duration,
    robot_fetched: Instant,
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
            cache_ttl,
            robot_fetched: Instant::now(),
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
        if status.is_some_and(|status| status.is_success()) {
            let value = self.error_count.fetch_add(1, Ordering::Relaxed);
            return min(BLOCK_COOL_OFF * (1 << value), MAX_BLOCK_COOL_OFF);
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
        self.robot_fetched.duration_since(Instant::now()) < self.cache_ttl
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
    cache: Arc<HashCache<String, CachedRobotFile>>,
}

impl RobotCache {
    #[must_use]
    pub fn new() -> Self {
        RobotCache {
            cache: Arc::new(HashCache::new()),
        }
    }

    pub async fn can_fetch(&mut self, domain: &str, url: &Url) -> Option<bool> {
        if let Some(occupied_entry) = self.cache.get_async(domain).await {
            return if occupied_entry.get().is_valid() {
                let _ = occupied_entry.remove();
                None
            } else {
                occupied_entry.get().can_fetch(url)
            };
        }
        None
    }

    pub async fn domain_delay(&mut self, status: Option<StatusCode>, domain: &str) -> Duration {
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
        let _ = self.cache.put_async(domain.to_owned(), file).await;
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

    if let Ok((data, status, _)) = fetch(client, &robot_url).await
        && status.is_success()
    {
        let robot = CachedRobotFile::new(ROBOTS_CACHE_TTL_SECONDS, Some(&data));
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
