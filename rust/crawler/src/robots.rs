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
        let robot = if status.is_client_error() && status.as_u16() != 429 {
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

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use reqwest::{StatusCode, Url};

    use super::{CachedRobotFile, RobotCache, get_robot_url, robot_allowed};
    use crate::{
        constants::{
            BLOCK_COOL_OFF, DEFAULT_DOMAIN_DELAY, MAX_BLOCK_COOL_OFF,
            ROBOTS_CACHE_ERROR_TTL_SECONDS, ROBOTS_CACHE_TTL_SECONDS,
        },
        test_support::{TestServer, client, text},
    };

    const ROBOTS: &str = "User-agent: mwmbl-crawler\nDisallow: /private\nCrawl-delay: 2\n";

    #[test]
    fn robots_txt_is_at_the_root_of_the_host() {
        let url = Url::parse("https://example.com:8080/a/b?q=1#frag").unwrap();
        assert_eq!(
            get_robot_url(&url).as_str(),
            "https://example.com:8080/robots.txt"
        );
    }

    #[test]
    fn a_robots_file_allows_or_denies_by_path() {
        let robot = CachedRobotFile::new(ROBOTS_CACHE_TTL_SECONDS, Some(ROBOTS.as_bytes()));
        let public = Url::parse("https://example.com/public").unwrap();
        let private = Url::parse("https://example.com/private/page").unwrap();
        assert_eq!(robot.can_fetch(&public), Some(true));
        assert_eq!(robot.can_fetch(&private), Some(false));
        assert_eq!(
            CachedRobotFile::new(ROBOTS_CACHE_ERROR_TTL_SECONDS, None).can_fetch(&public),
            None
        );
    }

    #[test]
    fn the_delay_is_the_crawl_delay_asked_for_or_the_default() {
        let with_delay = CachedRobotFile::new(ROBOTS_CACHE_TTL_SECONDS, Some(ROBOTS.as_bytes()));
        assert_eq!(
            with_delay.delay(Some(StatusCode::OK)),
            Duration::from_secs(2)
        );
        let without = CachedRobotFile::new(ROBOTS_CACHE_TTL_SECONDS, Some(b""));
        assert_eq!(without.delay(Some(StatusCode::OK)), DEFAULT_DOMAIN_DELAY);
        // No robots.txt to go by, so the domain waits out the error TTL.
        let unavailable = CachedRobotFile::new(ROBOTS_CACHE_ERROR_TTL_SECONDS, None);
        assert_eq!(
            unavailable.delay(Some(StatusCode::OK)),
            ROBOTS_CACHE_ERROR_TTL_SECONDS
        );
    }

    #[test]
    fn refusals_double_the_cool_off_up_to_the_maximum_and_a_success_resets_it() {
        let robot = CachedRobotFile::new(ROBOTS_CACHE_TTL_SECONDS, Some(b""));
        let refused = Some(StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(robot.delay(refused), BLOCK_COOL_OFF);
        assert_eq!(robot.delay(refused), BLOCK_COOL_OFF * 2);
        assert_eq!(robot.delay(refused), BLOCK_COOL_OFF * 4);
        for _ in 0..40 {
            robot.delay(refused);
        }
        assert_eq!(robot.delay(refused), MAX_BLOCK_COOL_OFF);

        assert_eq!(robot.delay(Some(StatusCode::OK)), DEFAULT_DOMAIN_DELAY);
        assert_eq!(robot.delay(refused), BLOCK_COOL_OFF);
    }

    #[tokio::test]
    async fn robots_txt_is_fetched_once_and_then_served_from_the_cache() {
        let server = TestServer::start(vec![("/robots.txt", text(200, ROBOTS))]).await;
        let client = client();
        let mut cache = RobotCache::new();

        let public = server.url("/public");
        let private = server.url("/private/page");
        assert!(
            robot_allowed(&public, "localhost", &client, &mut cache)
                .await
                .unwrap()
        );
        assert!(
            !robot_allowed(&private, "localhost", &client, &mut cache)
                .await
                .unwrap()
        );
        assert_eq!(server.requests(), vec!["/robots.txt"]);
        assert_eq!(
            cache.domain_delay(Some(StatusCode::OK), "localhost").await,
            Duration::from_secs(2)
        );
    }

    #[tokio::test]
    async fn a_missing_robots_txt_allows_everything() {
        let server = TestServer::start(vec![]).await;
        let mut cache = RobotCache::new();
        let allowed = robot_allowed(&server.url("/anything"), "localhost", &client(), &mut cache)
            .await
            .unwrap();
        assert!(allowed);
    }

    #[tokio::test]
    async fn a_robots_txt_that_fails_or_rate_limits_is_unavailable() {
        for status in [500, 503, 429] {
            let server = TestServer::start(vec![("/robots.txt", text(status, ""))]).await;
            let mut cache = RobotCache::new();
            let result =
                robot_allowed(&server.url("/page"), "localhost", &client(), &mut cache).await;
            assert!(result.is_err(), "status {status}");
            assert_eq!(
                cache.domain_delay(Some(StatusCode::OK), "localhost").await,
                ROBOTS_CACHE_ERROR_TTL_SECONDS,
                "status {status}"
            );
        }
    }
}
