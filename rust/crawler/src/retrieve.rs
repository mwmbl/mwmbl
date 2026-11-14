use std::{
    clone, future, result,
    sync::Arc,
    time::{Duration, Instant, SystemTimeError, UNIX_EPOCH},
};

use crate::{
    constants::{
        MAX_FETCH_SIZE, MAX_REDIRECTS, ROBOTS_CACHE_ERROR_TTL_SECONDS, ROBOTS_CACHE_TTL_SECONDS,
        TIMEOUT,
    },
    url_queue::ClaimedUrl,
};
use anyhow::bail;
use futures_util::StreamExt;
use reqwest::{Client, StatusCode, Url, header, redirect::Policy};
use scc::HashCache;
use texting_robots::Robot;

pub enum CrawlErrors {
    NoDomain {
        timestamp: Instant,
        url: Url,
    },
    RobotsUnavailable {
        timestamp: Instant,
        url: Url,
        err: anyhow::Error,
    },
    RobotsDenied {
        timestamp: Instant,
        url: Url,
    },
    AbortError {
        timestamp: Instant,
        url: Url,
        err: anyhow::Error,
    },
    NoResponseText {
        timestamp: Instant,
        url: Url,
        status: StatusCode,
        resolved_url: Url,
    },
    HtmlParsing {
        timestamp: Instant,
        url: Url,
        status: StatusCode,
        resolved_url: Url,
    },
    ParagraphParsing {
        timestamp: Instant,
        url: Url,
        status: StatusCode,
        resolved_url: Url,
    },
}

pub struct CrawlContent {
    timestamp: u128,
    url: Url,
    title: String,
    extract: String,
    status: StatusCode,
    links: Vec<Url>,
    extra_links: Vec<Url>,
}

#[derive(Clone)]
pub struct CachedRobotFile {
    cache_ttl: Duration,
    robot_fetched: Instant,
    robot: Option<Arc<Robot>>,
}

impl CachedRobotFile {
    pub fn new(cache_ttl: Duration, user_agent: &str, txt: Option<&[u8]>) -> Self {
        let robot = if let Some(txt) = txt
            && let Ok(robot) = Robot::new(user_agent, txt)
        {
            Some(Arc::new(robot))
        } else {
            None
        };
        Self {
            cache_ttl,
            robot_fetched: Instant::now(),
            robot,
        }
    }
    pub fn can_fetch(&self, url: &Url) -> Option<bool> {
        if let Some(robot) = &self.robot {
            return Some(robot.allowed(url.as_str()));
        }
        None
    }
    pub fn delay(&self) -> Option<f32> {
        if let Some(robot) = &self.robot {
            return robot.delay;
        }
        None
    }
    pub fn is_valid(&self) -> bool {
        self.robot_fetched.duration_since(Instant::now()) < self.cache_ttl
    }
}

#[derive(Clone)]
pub struct RobotCache {
    cache: Arc<HashCache<String, CachedRobotFile>>,
    user_agent: String,
}

impl RobotCache {
    #[must_use]
    pub fn new(user_agent: &str) -> Self {
        RobotCache {
            cache: Arc::new(HashCache::new()),
            user_agent: user_agent.to_owned(),
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

    pub async fn put(&mut self, domain: &str, file: CachedRobotFile) {
        let _ = self.cache.put_async(domain.to_owned(), file).await;
    }

    pub fn user_agent(&self) -> &str {
        &self.user_agent
    }
}

fn get_robot_url(url: &Url) -> Url {
    let mut robot_path = url.clone();
    robot_path.set_path("robots.txt");
    robot_path.set_query(None);
    robot_path.set_fragment(None);
    robot_path
}

async fn fetch(client: &Client, url: &Url) -> anyhow::Result<(Vec<u8>, StatusCode, Url)> {
    let r = client.get(url.clone()).send().await?;

    let status = r.status();
    let url = r.url().to_owned();

    let mut stream = r.bytes_stream();

    let mut data: Vec<u8> = Vec::new();

    while let Some(item) = stream.next().await {
        data.extend(item?);
        if data.len() > MAX_FETCH_SIZE {
            break;
        }
    }
    Ok((data, status, url))
}

async fn fetch_string(client: &Client, url: &Url) -> anyhow::Result<(String, StatusCode, Url)> {
    let (data, status, url) = fetch(client, url).await?;
    let data = match String::from_utf8(data) {
        Ok(str) => str,
        Err(err) => err.as_bytes().iter().map(|x| char::from(*x)).collect(),
    };
    Ok((data, status, url))
}

async fn robot_allowed(
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
        let robot = CachedRobotFile::new(
            ROBOTS_CACHE_TTL_SECONDS,
            &robots_cache.user_agent,
            Some(&data),
        );
        let allowed = robot.can_fetch(url).unwrap_or_default();
        robots_cache.put(domain, robot).await;
        return Ok(allowed);
    }
    robots_cache
        .put(
            domain,
            CachedRobotFile::new(
                ROBOTS_CACHE_ERROR_TTL_SECONDS,
                &robots_cache.user_agent,
                None,
            ),
        )
        .await;
    bail!("robots.txt unavailable for {domain}");
}

pub async fn crawl(
    claimed_url: ClaimedUrl,
    mut robots_cache: RobotCache,
    client: Client,
) -> Result<CrawlContent, CrawlErrors> {
    let url = claimed_url.url();
    let timestamp = Instant::now();
    let Some(domain) = url.domain() else {
        return Err(CrawlErrors::NoDomain {
            timestamp,
            url: url.clone(),
        });
    };

    match robot_allowed(url, domain, &client, &mut robots_cache).await {
        Ok(true) => {}
        Ok(false) => {
            return Err(CrawlErrors::RobotsDenied {
                timestamp,
                url: url.clone(),
            });
        }
        Err(e) => {
            return Err(CrawlErrors::RobotsUnavailable {
                timestamp,
                url: url.clone(),
                err: e,
            });
        }
    }

    let (data, status, resolved_url) = match fetch_string(&client, url).await {
        Ok(v) => v,
        Err(e) => {
            return Err(CrawlErrors::AbortError {
                timestamp,
                url: url.clone(),
                err: e,
            });
        }
    };

    if data.is_empty() {
        return Err(CrawlErrors::NoResponseText {
            timestamp,
            url: url.clone(),
            status,
            resolved_url,
        });
    }

    todo!();
}
