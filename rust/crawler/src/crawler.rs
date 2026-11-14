use std::{
    sync::Arc,
    time::{Duration, UNIX_EPOCH},
};

use mwmbl_core::blacklist_providers::BlacklistProvider;
use rand::{Rng, RngExt, random_range};
use reqwest::{Client, redirect::Policy};
use scc::{HashCache, HashMap};
use tokio::{spawn, sync::RwLock, time::sleep};

use crate::{
    CrawlerParams,
    constants::{API_PRODUCT, MAX_REDIRECTS, PROJECT_URL, TIMEOUT},
    curated_domains::{CuratedDomains, DefaultBlackList, get_default_blacklist_provider},
    retrieve::{CachedRobotFile, RobotCache, crawl},
    url_queue::{ClaimedUrl, SharedURLQueue},
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
            let cache = self.robots_cache.clone();
            let crawl_client = client.clone();
            let handle = spawn(async move { crawl(url, cache, crawl_client).await });

            match handle.await {
                Ok(result) => todo!(),
                Err(err) => eprintln!("Unexpected error while crawling: {err}"),
            }
            sleep(Duration::from_secs_f32(rng.random_range(
                0.9 * self.params.crawl_delay_seconds..1.1 * self.params.crawl_delay_seconds,
            )))
            .await;
        }
    }

    pub async fn run_once(&mut self) -> anyhow::Result<()> {
        todo!();
    }

    pub fn crawl_user_agent(contact_info: &str) -> String {
        format!(
            "{API_PRODUCT} (https://github.com/mwmbl/mwmbl/ contact {contact_info})"
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
        todo!()
    }
}
