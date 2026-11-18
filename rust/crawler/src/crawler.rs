use std::{
    cmp::{max, min},
    collections::VecDeque,
    sync::Arc,
    time::Duration,
};

use kreuzberg::utils::timing::Instant;
use mwmbl_core::blacklist_providers::BlacklistProvider;
use rand::RngExt;
use reqwest::redirect::Policy;
use tokio::{
    spawn,
    sync::{RwLock, Semaphore},
    task::yield_now,
    time::sleep,
};

use crate::{
    CrawlerParams,
    bloomdb::BloomDatabase,
    constants::{API_PRODUCT, MAX_DOMAIN_DELAY, MAX_REDIRECTS, PROJECT_URL, TIMEOUT},
    curated_domains::{CuratedDomains, DefaultBlackList, get_default_blacklist_provider},
    retrieve::{CrawlResult, crawl},
    robots::RobotCache,
    url_queue::SharedURLQueue,
};

#[derive(Clone)]
pub struct Crawler<T: BlacklistProvider = DefaultBlackList> {
    url_queue: SharedURLQueue<T>,
    // blacklist: T,
    username: Arc<RwLock<Option<String>>>,
    // error_last_reported_at: Arc<HashMap<String, f32>>,
    params: Arc<CrawlerParams>,
    robots_cache: RobotCache,
    domainlinkdb: Arc<BloomDatabase>,
    crawl_semaphore: Arc<Semaphore>,
    to_submit: Arc<RwLock<VecDeque<CrawlResult>>>,
}

impl Crawler {
    pub async fn new(params: CrawlerParams) -> anyhow::Result<Crawler> {
        let username = Arc::new(RwLock::new(None));
        let curated_domains = CuratedDomains::new(&params.remote_server);
        let blacklist = get_default_blacklist_provider(curated_domains.clone()).await?;
        let url_queue = SharedURLQueue::new(blacklist.clone(), curated_domains);
        let domainlinkdb = BloomDatabase::new(&params.home).await?;

        Ok(Crawler {
            url_queue,
            // blacklist,
            username,
            // error_last_reported_at: Arc::new(HashMap::default()),
            robots_cache: RobotCache::new(),
            crawl_semaphore: Arc::new(Semaphore::new(params.max_crawl_task)),
            params: Arc::new(params),
            domainlinkdb,
            to_submit: Arc::new(RwLock::new(VecDeque::new())),
        })
    }

    fn get_client(&self) -> anyhow::Result<reqwest::Client> {
        Ok(reqwest::Client::builder()
            .brotli(true)
            .deflate(true)
            .gzip(true)
            .zstd(true)
            .read_timeout(TIMEOUT)
            .user_agent(self.crawl_user_agent())
            .redirect(Policy::limited(MAX_REDIRECTS + 1))
            .pool_idle_timeout(Duration::ZERO)
            .pool_max_idle_per_host(0)
            .build()?)
    }

    pub async fn run(&mut self) -> anyhow::Result<()> {
        let client = self.get_client()?;
        let start = Instant::now();

        loop {
            let permit = self.crawl_semaphore.clone().acquire_owned().await?;
            let mut cache = self.robots_cache.clone();
            let crawl_client = client.clone();
            #[cfg(feature = "servo")]
            let user_agent = self.crawl_user_agent();
            let mut url_queue = self.url_queue.clone();
            let db = self.domainlinkdb.clone();
            let to_submit = self.to_submit.clone();

            spawn(async move {
                let url = url_queue.get_url().await;
                let permit = permit;

                let (url, result) = crawl(
                    url,
                    cache.clone(),
                    crawl_client,
                    #[cfg(feature = "servo")]
                    user_agent,
                )
                .await;

                drop(permit);

                let (resolved_domain, status) = result.get_resolved_info();

                if let Some(domain) = url.url().domain() {
                    let time_a = cache.domain_delay(status, domain).await;
                    let time_b = if let Some(resolved_domain) = resolved_domain
                        && resolved_domain != domain
                    {
                        cache.domain_delay(status, resolved_domain).await
                    } else {
                        Duration::ZERO
                    };
                    let time = max(time_a, time_b);
                    let time = min(MAX_DOMAIN_DELAY, time);
                    url.release_after_delay(resolved_domain, time);
                }

                url_queue.record_url_in_database(db, &result).await;

                if matches!(result, CrawlResult::CrawlContent { .. }) {
                    let mut lock = to_submit.write().await;
                    println!(
                        "speed: {}/s | crawled {}",
                        ((lock.len() as u128) + 1) / (1 + (start.elapsed_millis() / 1000)),
                        result.url().as_str()
                    );
                    lock.push_back(result);
                }
            });

            if self.params.crawl_delay_seconds > 0.0 {
                let random_time = rand::rng().random_range(
                    0.9 * self.params.crawl_delay_seconds..1.1 * self.params.crawl_delay_seconds,
                );
                sleep(Duration::from_secs_f32(random_time)).await;
            }
        }
    }

    pub async fn run_once(&mut self) -> anyhow::Result<()> {
        let client = self.get_client()?;

        let url = self.url_queue.get_url().await;
        let cache = self.robots_cache.clone();
        let crawl_client = client.clone();

        #[cfg(feature = "servo")]
        let user_agent = self.crawl_user_agent();
        let handle = spawn(async move {
            crawl(
                url,
                cache,
                crawl_client,
                #[cfg(feature = "servo")]
                user_agent,
            )
            .await
        });

        match handle.await {
            Ok((_claimed_url, result)) => println!("{result:#?}"),
            Err(err) => eprintln!("can't join handle: {err}"),
        }
        Ok(())
    }

    fn crawl_user_agent(&self) -> String {
        format!(
            "{API_PRODUCT} (https://github.com/mwmbl/mwmbl/ contact {})",
            self.params.contact_info
        )
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
