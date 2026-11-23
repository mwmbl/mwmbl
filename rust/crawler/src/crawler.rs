use anyhow::bail;
use mwmbl_core::{
    blacklist_providers::BlacklistProvider,
    indexer::{Document, DocumentState, IndexMode, TinyIndex},
};
use rand::RngExt;
use reqwest::header;
use serde_json::Value;
use std::{
    cmp::{Reverse, max, min},
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
    time::{Instant, sleep, sleep_until},
};

use crate::{
    CrawlerParams,
    batch::{PostResultsResponse, SendResult, SendResults},
    bloomdb::BloomDatabase,
    constants::{
        API_PRODUCT, MAX_DOMAIN_DELAY, MAX_SUBMIT_QUEUE_SIZE, PROJECT_URL, READ_TIMEOUT,
        SEND_TIMEOUT, SUBMIT_BATCH_SIZE, SUBMIT_TOP_N, UPGRADE_REQUIRED_WAIT,
    },
    curated_domains::{CuratedDomains, DefaultBlackList, get_default_blacklist_provider},
    path_creation::{get_device_name, healthcheck, index_path},
    retrieve::{CrawlResult, crawl},
    robots::RobotCache,
    ssrf::{PublicAddressResolver, redirect_policy},
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
    submit_paused_until: Arc<RwLock<Option<Instant>>>,
    count: Arc<AtomicUsize>,
    index: TinyIndex,
}

#[derive(Debug)]
enum Submission {
    Accepted(PostResultsResponse),
    /// The server's explanation of why it wants a newer crawler.
    UpgradeRequired(String),
}

async fn post_results(
    client: &reqwest::Client,
    remote_server: &str,
    results: &SendResults,
) -> anyhow::Result<Submission> {
    let Ok(response) = client
        .post(format!("{remote_server}/api/v1/crawler/results"))
        .json(results)
        .send()
        .await
    else {
        bail!("Error in sending result");
    };
    if response.status() == 426 {
        return Ok(Submission::UpgradeRequired(response.text().await?));
    }
    let Ok(response) = response.error_for_status() else {
        bail!("Error in sending result");
    };
    let Ok(response) = response.json::<PostResultsResponse>().await else {
        bail!("Error in parsing PostResultsResponse");
    };
    Ok(Submission::Accepted(response))
}

impl Crawler {
    pub async fn new(params: CrawlerParams) -> anyhow::Result<Crawler> {
        let username = Arc::new(RwLock::new(None));
        let curated_domains = CuratedDomains::new(&params.remote_server);
        let blacklist = get_default_blacklist_provider(curated_domains.clone()).await?;
        let url_queue = SharedURLQueue::new(blacklist.clone(), curated_domains);
        let domainlinkdb = BloomDatabase::new(&params.home).await?;
        let index_path = index_path(&params.home);
        let index = TinyIndex::from_file(&index_path, IndexMode::WRITE)?;

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
            submit_paused_until: Arc::new(RwLock::new(None)),
            index,
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
            .redirect(redirect_policy())
            .dns_resolver(PublicAddressResolver::system()?)
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
            let params = self.params.clone();

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
                        lock.len(),
                        result.url.as_str(),
                    );
                    lock.push_back(result.to_document());
                    if lock.len() > MAX_SUBMIT_QUEUE_SIZE {
                        lock.pop_front();
                    }
                    drop(lock);
                    // Crawling counts as alive, not just submitting: with a long crawl delay a
                    // batch takes longer to fill than the health check's window.
                    if healthcheck(&params.home).await.is_err() {
                        eprintln!("Failed to update healthfile");
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
        let submit_paused_until = self.submit_paused_until.clone();
        spawn(async move {
            loop {
                // Documents keep queueing (up to MAX_SUBMIT_QUEUE_SIZE) while paused, so the
                // newest are sent once the pause is over rather than drained into rejections.
                let paused_until = *submit_paused_until.read().await;
                if let Some(paused_until) = paused_until
                    && paused_until > Instant::now()
                {
                    sleep_until(paused_until).await;
                    continue;
                }
                let mut lock = to_submit.write().await;
                if lock.len() >= SUBMIT_BATCH_SIZE {
                    let docs: Vec<Document> = lock.drain(0..SUBMIT_BATCH_SIZE).collect();
                    drop(lock);

                    let Ok(permit) = semaphore.clone().acquire_owned().await else {
                        eprintln!("Could not aquire permit for indexing");
                        continue;
                    };
                    let clone = clone.clone();
                    spawn(async move {
                        println!("Sending results.");
                        if let Err(err) = clone.index_batch(docs).await {
                            eprintln!("Error during indexing : {err}");
                        }
                        drop(permit);
                    });
                } else {
                    drop(lock);
                    sleep(Duration::from_secs(1)).await;
                }
            }
        });
    }

    async fn index_batch(&self, docs: Vec<Document>) -> anyhow::Result<()> {
        let indexed_documents = self.index.index_documents(docs, &self.blacklist).await?;

        let mut term_new_doc_count: Vec<(&String, &usize)> =
            indexed_documents.new_page_doc_counts().iter().collect();
        term_new_doc_count.sort_by_key(|v| Reverse(v.1));

        let device_name = get_device_name(&self.params.home, self.params.device_name.clone()).await;

        let mut terms: Vec<String> = Vec::new();
        let mut result_items: Vec<SendResult> = Vec::new();

        for (term, _) in term_new_doc_count.into_iter().take(SUBMIT_TOP_N) {
            let Ok(docs) = self.index.retrieve::<Document>(term).await else {
                continue;
            };
            let mut items: Vec<SendResult> = docs
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
            result_items.append(&mut items);
        }

        let client = self.get_send_client().await?;
        let results = SendResults::new(
            result_items,
            Some(env!("CARGO_PKG_VERSION").to_owned()),
            Some(device_name.clone()),
            Some(true),
        );
        let response = match post_results(&client, &self.params.remote_server, &results).await? {
            Submission::Accepted(response) => response,
            Submission::UpgradeRequired(message) => {
                // Waiting gives an external updater (compose timer, systemd timer) time to
                // replace this crawler, where retrying every batch would hammer the server.
                eprintln!(
                    "The server requires a newer crawler: {message} Pausing submission for {} \
                     seconds instead of sending results the server would reject.",
                    UPGRADE_REQUIRED_WAIT.as_secs()
                );
                *self.submit_paused_until.write().await =
                    Some(Instant::now() + UPGRADE_REQUIRED_WAIT);
                return Ok(());
            }
        };
        let Some(new_items) = response.new_items() else {
            bail!("Missing new_items from reponse");
        };

        for term in terms {
            let page_index = self.index.get_key_page_index(&term);
            let new_term_remote_items: Vec<Document> = new_items
                .iter()
                .filter(|doc| doc.term() == Some(&term))
                .cloned()
                .collect();
            let pagedocs = HashMap::from([(page_index, new_term_remote_items)]);
            self.index.index_pages(pagedocs, Some(&term)).await?;
        }
        if healthcheck(&self.params.home).await.is_err() {
            eprintln!("Failed to update healthfile");
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

#[cfg(test)]
mod tests {
    use super::{Crawler, Submission, post_results};
    use crate::{
        batch::SendResults,
        test_support::{TestServer, client, text},
    };

    #[tokio::test]
    async fn an_upgrade_required_response_is_reported_with_the_servers_message() {
        let server = TestServer::start(vec![(
            "/api/v1/crawler/results",
            text(426, "Crawler 2.0.0 is too old, please upgrade."),
        )])
        .await;
        let remote_server = server.url("").as_str().trim_end_matches('/').to_owned();
        let results = SendResults::new(Vec::new(), None, None, Some(true));

        let submission = post_results(&client(), &remote_server, &results)
            .await
            .unwrap();

        assert!(
            matches!(&submission, Submission::UpgradeRequired(message) if message == "Crawler 2.0.0 is too old, please upgrade."),
            "{submission:?}"
        );
    }

    #[test]
    fn the_api_user_agent_names_the_version_and_the_url_encoded_username() {
        let version = env!("CARGO_PKG_VERSION");
        assert_eq!(
            Crawler::api_user_agent(None),
            format!("mwmbl-crawler/{version} (+https://github.com/mwmbl/mwmbl)")
        );
        assert_eq!(
            Crawler::api_user_agent(Some(&"jo bloggs".to_owned())),
            format!("mwmbl-crawler/{version} (+https://github.com/mwmbl/mwmbl; user jo%20bloggs)")
        );
    }
}
