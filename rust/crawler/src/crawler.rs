use std::{
    cmp::{max, min},
    sync::Arc,
    time::Duration,
};

use mwmbl_core::blacklist_providers::BlacklistProvider;
use rand::RngExt;
use reqwest::redirect::Policy;
use scc::HashMap;
use tokio::{spawn, sync::RwLock, task::yield_now, time::sleep};

use crate::{
    CrawlerParams,
    constants::{
        API_PRODUCT, DEFAULT_DOMAIN_DELAY, MAX_DOMAIN_DELAY, MAX_REDIRECTS, PROJECT_URL, TIMEOUT,
    },
    curated_domains::{CuratedDomains, DefaultBlackList, get_default_blacklist_provider},
    retrieve::{RobotCache, crawl},
    url_queue::SharedURLQueue,
};

pub struct Crawler<T: BlacklistProvider = DefaultBlackList> {
    url_queue: SharedURLQueue<T>,
    blacklist: T,
    username: Arc<RwLock<Option<String>>>,
    error_last_reported_at: Arc<HashMap<String, f32>>,
    params: Arc<CrawlerParams>,
    robots_cache: RobotCache,
}

impl Crawler {
    pub async fn new(params: CrawlerParams) -> anyhow::Result<Crawler> {
        let username = Arc::new(RwLock::new(None));
        let curated_domains = CuratedDomains::new(&params.remote_server);
        let blacklist = get_default_blacklist_provider(curated_domains.clone()).await?;
        let url_queue = SharedURLQueue::new(blacklist.clone(), curated_domains);

        Ok(Crawler {
            url_queue,
            blacklist,
            username,
            error_last_reported_at: Arc::new(HashMap::default()),
            robots_cache: RobotCache::new(&Self::crawl_user_agent(&params.contact_info)),
            params: Arc::new(params),
        })
    }

    pub async fn run(&mut self) -> anyhow::Result<()> {
        let mut rng = rand::rng();
        let user_agent = Crawler::crawl_user_agent(&self.params.contact_info);

        let client = reqwest::Client::builder()
            .brotli(true)
            .deflate(true)
            .gzip(true)
            .zstd(true)
            .read_timeout(TIMEOUT)
            .user_agent(user_agent)
            .redirect(Policy::limited(MAX_REDIRECTS + 1))
            .build()?;

        loop {
            let url = self.url_queue.get_url().await;
            let mut cache = self.robots_cache.clone();
            let crawl_client = client.clone();
            spawn(async move {
                let (url, result) = crawl(url, cache.clone(), crawl_client).await;

                let resolved_domain = match &result {
                        crate::retrieve::CrawlResults::NoDomainError {
                            timestamp: _,
                            url: _,
                        }
                        | crate::retrieve::CrawlResults::RobotsDeniedError {
                            timestamp: _,
                            url: _,
                        }
                        | crate::retrieve::CrawlResults::RobotsUnavailableError {
                            timestamp: _,
                            url: _,
                            err: _,
                        }
                        | crate::retrieve::CrawlResults::AbortError {
                            timestamp: _,
                            url: _,
                            err: _,
                        } => None,
                        crate::retrieve::CrawlResults::NoResponseTextError {
                            timestamp: _,
                            url: _,
                            resolved_url,
                        }
                        | crate::retrieve::CrawlResults::HtmlParsingError {
                            timestamp: _,
                            url: _,
                            resolved_url,
                            err: _,
                        }
                        | crate::retrieve::CrawlResults::ParagraphParsingError {
                            timestamp: _,
                            url: _,
                            resolved_url,
                        } => resolved_url.domain(),
                        crate::retrieve::CrawlResults::CrawlContent { timestamp, url, resolved_url, title, extract, links, extra_links }=> resolved_url.domain(),
               
                };

                if let Some(domain) = url.url().domain() {
                    let time_a = cache.domain_delay(result.is_error(), domain).await;
                    let time_b = if let Some(resolved_domain) = resolved_domain
                        && resolved_domain != domain
                    {
                        cache.domain_delay(result.is_error(), domain).await
                    } else {
                        None
                    };
                    let time = max(
                        time_a.unwrap_or(Duration::ZERO),
                        time_b.unwrap_or(DEFAULT_DOMAIN_DELAY),
                    );
                    let time = min(MAX_DOMAIN_DELAY, time);
                    println!("{time:#?}");
                    url.release_after_delay(resolved_domain, time);
                }
                println!("{result:#?}");
                todo!()
                // TODO record_urls_in_database
            });

            if self.params.crawl_delay_seconds > 0.0 {
                sleep(Duration::from_secs_f32(rng.random_range(
                    0.9 * self.params.crawl_delay_seconds..1.1 * self.params.crawl_delay_seconds,
                )))
                .await;
            }
        }
    }

    pub async fn run_once(&mut self) -> anyhow::Result<()> {
        let user_agent = Crawler::crawl_user_agent(&self.params.contact_info);

        let client = reqwest::Client::builder()
            .brotli(true)
            .deflate(true)
            .gzip(true)
            .zstd(true)
            .read_timeout(TIMEOUT)
            .user_agent(user_agent)
            .redirect(Policy::limited(MAX_REDIRECTS + 1))
            .build()?;

        let url = self.url_queue.get_url().await;
        let cache = self.robots_cache.clone();
        let crawl_client = client.clone();
        let handle = spawn(async move { crawl(url, cache, crawl_client).await });

        match handle.await {
            Ok((_claimed_url, result)) => println!("{result:#?}"),
            Err(err) => eprintln!("can't join handle: {err}"),
        }
        Ok(())
    }

    pub fn crawl_user_agent(contact_info: &str) -> String {
        format!("{API_PRODUCT} (https://github.com/mwmbl/mwmbl/ contact {contact_info})")
    }

    pub fn api_user_agent(username: Option<&String>) -> String {
        let identity = if let Some(username) = username {
            format!("+{}; user {}", PROJECT_URL, urlencoding::encode(username))
        } else {
            format!("+{PROJECT_URL}")
        };
        format!("{API_PRODUCT} ({identity})")
    }

    pub async fn fetch_username(_username: &mut Arc<RwLock<Option<String>>>) -> Option<String> {
        yield_now().await;
        todo!()
    }
}
