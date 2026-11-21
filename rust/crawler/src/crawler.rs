use anyhow::bail;
use mwmbl_core::{
    blacklist_providers::BlacklistProvider,
    indexer::{Document, DocumentState, IndexMode, TinyIndex},
};
use rand::RngExt;
use reqwest::{header, redirect::Policy};
use serde_json::Value;
use std::{
    cmp::{max, min},
    collections::{HashMap, VecDeque},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    spawn,
    sync::{RwLock, Semaphore},
    time::{Instant, sleep},
};

use crate::{
    CrawlerParams,
    batch::{PostResultsResponse, SendResult, SendResults},
    bloomdb::BloomDatabase,
    constants::{
        API_PRODUCT, MAX_DOMAIN_DELAY, MAX_REDIRECTS, MAX_SUBMIT_QUEUE_SIZE, PROJECT_URL,
        READ_TIMEOUT, SEND_TIMEOUT, SUBMIT_BATCH_SIZE, SUBMIT_TOP_N,
    },
    curated_domains::{CuratedDomains, DefaultBlackList, get_default_blacklist_provider},
    path_creation::{get_device_name, index_path},
    retrieve::{CrawlResult, crawl},
    robots::RobotCache,
    url_queue::SharedURLQueue,
};

#[derive(Clone)]
pub struct Crawler<T: BlacklistProvider = DefaultBlackList> {
    url_queue: SharedURLQueue<T>,
    blacklist: T,
    username: Arc<RwLock<Option<String>>>,
    params: Arc<CrawlerParams>,
    robots_cache: RobotCache,
    domainlinkdb: Arc<BloomDatabase>,
    crawl_semaphore: Arc<Semaphore>,
    submit_semaphore: Arc<Semaphore>,
    to_submit: Arc<RwLock<VecDeque<Document>>>,
    count: Arc<AtomicUsize>,
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
            blacklist,
            username,
            robots_cache: RobotCache::new(),
            crawl_semaphore: Arc::new(Semaphore::new(params.max_crawl_task)),
            submit_semaphore: Arc::new(Semaphore::new(params.max_submit_task)),
            params: Arc::new(params),
            domainlinkdb,
            count: Arc::new(AtomicUsize::new(0)),
            to_submit: Arc::new(RwLock::new(VecDeque::new())),
        })
    }

    fn get_client(&self) -> anyhow::Result<reqwest::Client> {
        Ok(reqwest::Client::builder()
            .brotli(true)
            .deflate(true)
            .gzip(true)
            .zstd(true)
            .read_timeout(READ_TIMEOUT)
            .user_agent(self.crawl_user_agent())
            .redirect(Policy::limited(MAX_REDIRECTS + 1))
            .hickory_dns(true)
            .build()?)
    }

    pub async fn run(&mut self) -> anyhow::Result<()> {
        let client = self.get_client()?;
        let start = Instant::now();

        self.index();

        loop {
            let permit = self.crawl_semaphore.clone().acquire_owned().await?;
            let cache = self.robots_cache.clone();
            let crawl_client = client.clone();
            #[cfg(feature = "servo")]
            let user_agent = self.crawl_user_agent();
            let mut url_queue = self.url_queue.clone();
            let db = self.domainlinkdb.clone();
            let to_submit = self.to_submit.clone();
            let count = self.count.clone();

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

                if let CrawlResult::CrawlContent(result) = result {
                    let mut lock = to_submit.write().await;
                    let count = count.fetch_add(1, Ordering::SeqCst);
                    println!(
                        "speed: {}/s | queue {} | crawled {}",
                        ((count as u64) + 1) / (1 + start.elapsed().as_secs()),
                        result.url.as_str(),
                        lock.len()
                    );
                    lock.push_back(result.to_document());
                    if lock.len() > MAX_SUBMIT_QUEUE_SIZE {
                        lock.pop_front();
                    }
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

    pub fn index(&self) {
        let clone = self.clone();
        let semaphore = self.submit_semaphore.clone();
        let to_submit = self.to_submit.clone();
        spawn(async move {
            loop {
                let mut lock = to_submit.write().await;
                if lock.len() >= SUBMIT_BATCH_SIZE {
                    let docs: Vec<Document> = lock
                        .drain(0..SUBMIT_BATCH_SIZE).collect();
                    drop(lock);

                    let Ok(permit) = semaphore.clone().acquire_owned().await else {
                        eprintln!("Could not aquire permit for indexing");
                        continue;
                    };
                    let clone = clone.clone();
                    spawn(async move {
                        println!("Sending results.");
                        let _ = clone.index_batch(docs).await;
                        drop(permit);
                    });
                } else {
                    drop(lock);
                    sleep(Duration::from_secs(1)).await;
                    continue;
                }
            }
        });
    }

    async fn index_batch(&self, docs: Vec<Document>) -> anyhow::Result<()> {
        let index_path = index_path(&self.params.home);
        let index = TinyIndex::from_file(&index_path, IndexMode::WRITE)?;
        let indexed_documents = index.index_documents(docs, &self.blacklist).await?;

        let mut term_new_doc_count: Vec<(&String, &usize)> =
            indexed_documents.new_page_doc_counts().iter().collect();
        term_new_doc_count.sort_by_key(|v| v.1);

        let device_name = get_device_name(&self.params.home, self.params.device_name.clone());

        let mut terms: Vec<String> = Vec::new();
        let result_items: Vec<SendResult> = term_new_doc_count
            .into_iter()
            .take(SUBMIT_TOP_N)
            .filter_map(|(term, _)| {
                let Ok(docs) = index.retrieve::<Document>(term) else {
                    return None;
                };
                let result_items: Vec<SendResult> = docs
                    .into_iter()
                    .filter_map(|doc| {
                        if matches!(doc.state(), Some(DocumentState::SyncedWithMainIndex)) {
                            None
                        } else {
                            Some(SendResult::new(&doc))
                        }
                    })
                    .collect();

                terms.push(term.to_owned());
                Some(result_items)
            })
            .flatten()
            .collect();

        let client = self.get_send_client().await?;
        let results = SendResults::new(
            result_items,
            Some(env!("CARGO_PKG_VERSION").to_owned()),
            Some(device_name.clone()),
            Some(true),
        );
        let response = client
            .post(format!(
                "{}/api/v1/crawler/results",
                self.params.remote_server
            ))
            .json(&results)
            .send()
            .await;

        let Ok(response) = response else {
            bail!("Error in sending result");
        };
        let Ok(response) = response.error_for_status() else {
            bail!("Error in sending result");
        };
        let Ok(response) = response.json::<PostResultsResponse>().await else {
            bail!("Error in parsing PostResultsResponse");
        };
        let Some(new_items) = response.new_items() else {
            bail!("Missing new_items from reponse");
        };

        for term in terms {
            let page_index = index.get_key_page_index(&term);
            let new_term_remote_items: Vec<Document> = new_items
                .iter()
                .filter(|doc| doc.term() == Some(&term))
                .cloned()
                .collect();
            let pagedocs = HashMap::from([(page_index, new_term_remote_items)]);
            index.index_pages(pagedocs, true)?;
        }
        println!("Results sendt.");
        Ok(())
    }

    async fn get_send_client(&self) -> anyhow::Result<reqwest::Client> {
        let mut headers = header::HeaderMap::new();
        let Ok(mut auth_value) = header::HeaderValue::from_str(&self.params.api_key) else {
            bail!("Invalid api key header");
        };
        auth_value.set_sensitive(true);
        headers.insert("X-API-Key", auth_value);

        let client = reqwest::Client::builder()
            .brotli(true)
            .deflate(true)
            .gzip(true)
            .zstd(true)
            .timeout(SEND_TIMEOUT)
            .default_headers(headers)
            .user_agent(Self::api_user_agent(self.fetch_username().await.as_ref()))
            .build()?;
        Ok(client)
    }

    pub async fn fetch_username(&self) -> Option<String> {
        if let Some(username) = self.username.read().await.clone() {
            return Some(username);
        }
        let Ok(response) = reqwest::Client::default()
            .get(format!(
                "{}/api/v1/platform/user",
                self.params.remote_server
            ))
            .header("User-Agent", Self::api_user_agent(None))
            .header("X-API-Key", &self.params.api_key)
            .timeout(Duration::from_secs(10))
            .send()
            .await
        else {
            return None;
        };

        let Ok(response) = response.error_for_status() else {
            return None;
        };

        let Ok(response) = response.json::<Value>().await else {
            return None;
        };
        match response {
            Value::Object(map) => match map.get("username") {
                Some(Value::String(s)) => {
                    Some(self.username.write().await.insert(s.to_owned()).to_owned())
                }
                _ => None,
            },
            _ => None,
        }
    }
}
