use std::{
    cmp::{max, min},
    collections::VecDeque,
    sync::{Arc, atomic::{AtomicU64, AtomicUsize, Ordering}},
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
    CrawlerParams, memstats,
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
    //username: Arc<RwLock<Option<String>>>,
    // error_last_reported_at: Arc<HashMap<String, f32>>,
    params: Arc<CrawlerParams>,
    robots_cache: RobotCache,
    domainlinkdb: Arc<BloomDatabase>,
    crawl_semaphore: Arc<Semaphore>,
    //to_submit: Arc<RwLock<VecDeque<CrawlResult>>>,
    count: Arc<AtomicUsize>
}

impl Crawler {
    pub async fn new(params: CrawlerParams) -> anyhow::Result<Crawler> {
        //let username = Arc::new(RwLock::new(None));
        let curated_domains = CuratedDomains::new(&params.remote_server);
        let blacklist = get_default_blacklist_provider(curated_domains.clone()).await?;
        let url_queue = SharedURLQueue::new(blacklist.clone(), curated_domains);
        let domainlinkdb = BloomDatabase::new(&params.home).await?;

        Ok(Crawler {
            url_queue,
            // blacklist,
            //username,
            // error_last_reported_at: Arc::new(HashMap::default()),
            robots_cache: RobotCache::new(),
            crawl_semaphore: Arc::new(Semaphore::new(params.max_crawl_task)),
            params: Arc::new(params),
            domainlinkdb,
            //to_submit: Arc::new(RwLock::new(VecDeque::new())),
            count: Arc::new(AtomicUsize::new(0))
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
            .hickory_dns(true)
            .build()?)
    }

    pub async fn run(&mut self) -> anyhow::Result<()> {
        let client = self.get_client()?;
        let start = Instant::now();
        self.spawn_mem_reporter();

        loop {
            let permit = self.crawl_semaphore.clone().acquire_owned().await?;
            let mut cache = self.robots_cache.clone();
            let crawl_client = client.clone();
            #[cfg(feature = "servo")]
            let user_agent = self.crawl_user_agent();
            let mut url_queue = self.url_queue.clone();
            let db = self.domainlinkdb.clone();
            //let to_submit = self.to_submit.clone();
            let count = self.count.clone();

            spawn(async move {
                let _task_gauge = memstats::Gauge::new(&memstats::CRAWL_TASKS, 1);
                let get_url_gauge = memstats::Gauge::new(&memstats::IN_GET_URL, 1);
                let url = url_queue.get_url().await;
                drop(get_url_gauge);
                let permit = permit;

                let crawl_gauge = memstats::Gauge::new(&memstats::IN_CRAWL, 1);
                let (url, result) = crawl(
                    url,
                    cache.clone(),
                    crawl_client,
                    #[cfg(feature = "servo")]
                    user_agent,
                )
                .await;
                drop(crawl_gauge);

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
                    //let mut lock = to_submit.write().await;
                    let count = count.fetch_add(1, Ordering::SeqCst);
                    println!(
                        "speed: {}/s | crawled {}",
                        ((count as u128) + 1) / (1 + (start.elapsed_millis() / 1000)),
                        result.url().as_str()
                    );
                    //lock.push_back(result);
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

    fn spawn_mem_reporter(&self) {
        let url_queue = self.url_queue.clone();
        let robots_cache = self.robots_cache.clone();
        let semaphore = self.crawl_semaphore.clone();
        let max_tasks = self.params.max_crawl_task;
        let count = self.count.clone();
        let interval: u64 = std::env::var("MEMSTATS_INTERVAL_SECS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(10);
        #[cfg(feature = "memprof")]
        let dump_dir = std::env::var("MEMPROF_DUMP_DIR").ok();
        #[cfg(feature = "memprof")]
        let dump_every: u64 = std::env::var("MEMPROF_DUMP_EVERY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(6);
        spawn(async move {
            let start = Instant::now();
            let mut tick: u64 = 0;
            loop {
                sleep(Duration::from_secs(interval)).await;
                tick += 1;
                let q = url_queue.debug_sizes().await;
                let metrics = tokio::runtime::Handle::current().metrics();
                #[cfg(feature = "memprof")]
                let (alloc, resident) = memstats::jemalloc::allocated_resident();
                #[cfg(not(feature = "memprof"))]
                let (alloc, resident) = (0usize, 0usize);
                let mb = |b: u64| b / (1024 * 1024);
                eprintln!(
                    "[memstats] t={}s rss={}MB jemalloc_alloc={}MB jemalloc_resident={}MB \
                     tokio_alive={} crawl_tasks={} in_get_url={} in_crawl={} permits_used={} \
                     fetch_bytes={}MB release_pending={} add_urls_pending={} add_urls_pending_urls={} \
                     domain_urls={} domain_urls_total_urls={} domain_scores={} domain_claims={} \
                     robots_cache={} robots_fetches={} page_fetches={} crawled={}",
                    start.elapsed_millis() / 1000,
                    mb(memstats::rss_bytes()),
                    mb(alloc as u64),
                    mb(resident as u64),
                    metrics.num_alive_tasks(),
                    memstats::get(&memstats::CRAWL_TASKS),
                    memstats::get(&memstats::IN_GET_URL),
                    memstats::get(&memstats::IN_CRAWL),
                    max_tasks - semaphore.available_permits(),
                    memstats::get(&memstats::FETCH_BYTES) / (1024 * 1024),
                    memstats::get(&memstats::RELEASE_PENDING),
                    memstats::get(&memstats::ADD_URLS_PENDING),
                    memstats::get(&memstats::ADD_URLS_PENDING_URLS),
                    q.domain_urls,
                    q.domain_urls_total_urls,
                    q.domain_scores,
                    q.domain_claims,
                    robots_cache.len(),
                    memstats::get(&memstats::ROBOTS_FETCHES),
                    memstats::get(&memstats::PAGE_FETCHES),
                    count.load(Ordering::Relaxed),
                );
                #[cfg(feature = "memprof")]
                if let Some(dir) = &dump_dir
                    && tick % dump_every == 0
                {
                    memstats::jemalloc::dump_profile(&format!(
                        "{dir}/heap.{:06}.heap",
                        start.elapsed_millis() / 1000
                    ));
                }
            }
        });
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
