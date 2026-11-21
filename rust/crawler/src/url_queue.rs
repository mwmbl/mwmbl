use std::{
    hash::{BuildHasher, RandomState},
    sync::{Arc, Weak},
    time::Duration,
};

use chrono::{NaiveTime, Utc};
use itertools::Itertools;
use mwmbl_core::{
    blacklist_providers::BlacklistProvider, constants::CORE_DOMAINS,
    hn_top_domains_filtered::DOMAINS,
};
use ordered_float::OrderedFloat;
use priority_queue::DoublePriorityQueue;
use rand::{RngExt, seq::IndexedRandom};
use reqwest::Url;
use scc::HashMap;
use tokio::{
    spawn,
    sync::{Mutex, OwnedMutexGuard, RwLock},
    task::{self, yield_now},
    time::{Instant, sleep, timeout},
};

use crate::{
    bloomdb::{BloomDatabase, FoundURL, UrlStatus},
    constants::{
        DOMAIN_TIMEOUT, DOMAINS_VEC, KNOWN_DOMAINS_RANDOM_WEIGHT, MAX_DOMAINS_PER_QUEUE,
        MAX_TIME_DELTA, MAX_URL_PER_DOMAINS, SCORE_TIME_CONSTANT, SEED_DOMAIN_RANDOM_WEIGHT,
        TOP_CURATED_DOMAIN_RANDOM_WEIGHT, TOP_OTHER_DOMAIN_RANDOM_WEIGHT,
    },
    curated_domains::CuratedDomains,
    retrieve::CrawlResult,
};

type DomainQueue = DoublePriorityQueue<String, OrderedFloat<f32>>;
type UrlQueue = DoublePriorityQueue<Url, OrderedFloat<f32>>;
type Claims = HashMap<String, Arc<Mutex<(String, UrlQueue, Instant)>>>;

#[derive(Clone)]
pub struct SharedURLQueue<T: BlacklistProvider, const S: usize = 1000> {
    blacklist_provider: T,
    curated_domains: CuratedDomains,
    domain_claims: Arc<Claims>,
    domain_scores: Arc<[RwLock<DomainQueue>; S]>,
    build_hasher: Arc<RandomState>,
}
pub struct ClaimedDomain {
    keep_avaliable: bool,
    domain_lock: OwnedMutexGuard<(String, UrlQueue, Instant)>,
    domain_claims: Weak<Claims>,
}

impl ClaimedDomain {
    #[must_use]
    pub fn new(
        domain_claims: Weak<Claims>,
        mut domain_lock: OwnedMutexGuard<(String, UrlQueue, Instant)>,
        keep_avaliable: bool,
    ) -> ClaimedDomain {
        domain_lock.2 = Instant::now();
        ClaimedDomain {
            keep_avaliable,
            domain_lock,
            domain_claims,
        }
    }
}

impl Drop for ClaimedDomain {
    fn drop(&mut self) {
        if !self.keep_avaliable
            && let Some(domain_claims) = self.domain_claims.upgrade()
        {
            let domain = self.domain_lock.to_owned();
            spawn(async move {
                sleep(DOMAIN_TIMEOUT).await;
                domain_claims
                    .remove_if_async(&domain.0, |v| v.try_lock().is_ok_and(|v| v.2 == domain.2))
                    .await;
            });
        }
    }
}

pub struct ClaimedUrl {
    claim: ClaimedDomain,
    url: reqwest::Url,
    released: bool,
}

impl ClaimedUrl {
    pub fn url(&self) -> &reqwest::Url {
        &self.url
    }
    pub fn release_after_delay(self, resolved_domain: Option<&str>, delay: Duration) {
        let resolved_domain = resolved_domain.map(std::borrow::ToOwned::to_owned).clone();
        task::spawn(async move {
            let mut claimed_url = self;
            if let Some(domain) = claimed_url.url.domain() {
                let other_claim: Option<ClaimedDomain> = if let Some(resolved_domain) =
                    resolved_domain
                    && resolved_domain != domain
                    && let Some(claims) = claimed_url.claim.domain_claims.upgrade()
                {
                    let res = arc_claim_domain(claims, &resolved_domain).await;
                    if res.is_err() {
                        eprintln!("Error: Cannot lock resolved_domain.");
                    }
                    res.ok()
                } else {
                    None
                };

                sleep(delay).await;
                claimed_url.released = true;
                drop(other_claim);
            } else {
                eprintln!("Error: domain not found.");
            }
            drop(claimed_url);
        });
    }
}

impl Drop for ClaimedUrl {
    fn drop(&mut self) {
        if !self.released {
            eprintln!("Error: URL dropped without proper release");
        }
    }
}

async fn arc_claim_domain(
    domain_claims: Arc<Claims>,
    domain: &str,
) -> Result<ClaimedDomain, tokio::sync::TryLockError> {
    let entry = domain_claims.entry_async(domain.to_owned()).await;
    let mutex = match entry {
        scc::hash_map::Entry::Occupied(occupied_entry) => {
            let mutex = occupied_entry.get().clone();
            mutex.try_lock_owned()?
        }
        scc::hash_map::Entry::Vacant(vacant_entry) => {
            let mutex = Arc::new(Mutex::new((
                domain.to_owned(),
                UrlQueue::new(),
                Instant::now(),
            )));
            vacant_entry
                .insert_entry(mutex)
                .get()
                .clone()
                .try_lock_owned()?
        }
    };
    Ok(ClaimedDomain::new(
        Arc::downgrade(&domain_claims),
        mutex,
        false,
    ))
}

impl<T: BlacklistProvider, const S: usize> SharedURLQueue<T, S> {
    pub fn new(blacklist_provider: T, curated_domains: CuratedDomains) -> SharedURLQueue<T, S> {
        SharedURLQueue {
            blacklist_provider,
            //domain_urls: Arc::new(HashMap::default()),
            domain_claims: Arc::new(HashMap::default()),
            curated_domains,
            domain_scores: Arc::new(std::array::from_fn(|_| RwLock::new(DomainQueue::new()))),
            build_hasher: Arc::new(RandomState::new()),
        }
    }

    pub async fn claim_domain(
        &self,
        domain: &str,
    ) -> Result<ClaimedDomain, tokio::sync::TryLockError> {
        arc_claim_domain(self.domain_claims.clone(), domain).await
    }

    pub async fn get_url(&mut self) -> ClaimedUrl {
        loop {
            // loop until we can claim a domain, if claim fails we yield to allow other computation to add new domains
            let total_seed = SEED_DOMAIN_RANDOM_WEIGHT + KNOWN_DOMAINS_RANDOM_WEIGHT;
            let p_seed: usize = rand::rng().random_range(0..total_seed);

            let total_url_group: usize = TOP_OTHER_DOMAIN_RANDOM_WEIGHT
                + TOP_CURATED_DOMAIN_RANDOM_WEIGHT
                + CORE_DOMAINS.len();
            let p_url_group: usize = rand::rng().random_range(0..total_url_group);

            if p_seed < SEED_DOMAIN_RANDOM_WEIGHT {
                // SELECT A SEED DOMAIN
                let curated_non_static_domains_ref = self.curated_domains.get_non_static_domains();
                let curated_non_static_domains = curated_non_static_domains_ref.read().await;

                let total_seed_condidates = DOMAINS_VEC.len() + curated_non_static_domains.len();
                let p_seed_condidates: usize = rand::rng().random_range(0..total_seed_condidates);

                let random_domain = if p_seed_condidates < DOMAINS_VEC.len() {
                    DOMAINS_VEC.sample(&mut rand::rng(), 1).next().cloned()
                } else {
                    curated_non_static_domains
                        .sample(&mut rand::rng(), 1)
                        .next()
                        .cloned()
                };
                drop(curated_non_static_domains);
                if let Some(random_domain) = random_domain
                    && !self
                        .blacklist_provider
                        .is_domain_blacklisted(&random_domain)
                        .await
                    && let Ok(url) = Url::parse(&format!("https://{random_domain}/"))
                    && let Some(random_domain) = url.domain()
                    && let Ok(domain_lock) = self.claim_domain(random_domain).await
                {
                    return ClaimedUrl {
                        url,
                        released: false,
                        claim: domain_lock,
                    };
                }
            } else if p_url_group
                < (TOP_OTHER_DOMAIN_RANDOM_WEIGHT + TOP_CURATED_DOMAIN_RANDOM_WEIGHT)
            {
                // SELECT EITHER A TOP_CURATED_DOMAIN OR A TOP_DOMAIN NON CURATED
                let select_from_curated = p_url_group >= TOP_OTHER_DOMAIN_RANDOM_WEIGHT;
                let samples = self.domain_scores.sample(&mut rand::rng(), S);
                let mut domain: Option<String> = None;
                for queue in samples {
                    let queue_lock = queue.read().await;
                    let top_domain = if let Some((top_domain, _)) = queue_lock.peek_max() {
                        top_domain.to_owned()
                    } else {
                        continue;
                    };
                    if self
                        .blacklist_provider
                        .is_domain_blacklisted(&top_domain)
                        .await
                    {
                        drop(queue_lock);
                        queue.write().await.pop_max();
                        self.domain_claims.remove_async(&top_domain).await;
                        continue;
                    }
                    if (DOMAINS.contains_key(&top_domain)
                        || self.curated_domains.is_domain_curated(&top_domain).await)
                        == select_from_curated
                    {
                        domain = Some(top_domain);
                        break;
                    }
                }
                if let Some(domain) = domain
                    && let Some(claimed_url) = self.claim_url(&domain).await
                {
                    return claimed_url;
                }
            } else {
                // SELECT A CORE_DOMAINS
                let domain = CORE_DOMAINS.sample(&mut rand::rng(), 1).next().copied();
                if let Some(domain) = domain
                    && !self.blacklist_provider.is_domain_blacklisted(domain).await
                    && let Some(claimed_url) = self.claim_url(domain).await
                {
                    return claimed_url;
                }
            }
            yield_now().await;
        }

        // data struct:
        // domain -> urls (max 10k other domains + curated + )
        // urls -> score
        // domain score = max(domain_urls_score)
        // select top  domain
        // select top url per domain
        // 1% -> base url (https://{domain}/) of random non-blacklisted seed domain
        // 99% ->
        //    9  /159  random core domain                           (no matter the score)
        //    50 /159  curated orhn_top_domains_filtered domain     (sample randomly from them, among top 2k)
        //    100/159  other domains                                (sample randomly from them, among top 2k)
        // max: 2k (reduce?) double_priority_queue selected by domain hash https://docs.rs/priority-queue/latest/priority_queue/double_priority_queue/struct.DoublePriorityQueue.html
        // max 10k total domains
        // double_priority_queue only ever hold domain avaliable to take!! when adding : await the time
        // each domain hold a double_priority_queue with capacity
    }

    async fn claim_url(&mut self, domain: &str) -> Option<ClaimedUrl> {
        if let Some(domain_queue) = self.get_priority_queue(domain) {
            domain_queue.write().await.remove(domain);
        }
        if let Ok(mut domain_lock) = self.claim_domain(domain).await
            && let Some((url, _)) = domain_lock.domain_lock.1.pop_max()
        {
            return Some(ClaimedUrl {
                url,
                released: false,
                claim: domain_lock,
            });
        }
        None
    }

    fn get_priority_queue(&self, domain: &str) -> Option<&RwLock<DomainQueue>> {
        self.domain_scores
            .get(Self::get_priority_queue_index(&self.build_hasher, domain))
    }

    #[allow(clippy::cast_possible_truncation)]
    fn get_priority_queue_index(build_hasher: &RandomState, domain: &str) -> usize {
        build_hasher.hash_one(domain) as usize % S
    }

    pub async fn record_url_in_database(&self, db: Arc<BloomDatabase>, result: &CrawlResult) {
        let url_status = UrlStatus::from(result);
        let timestamp = *result.timestamp();

        let mut urls: Vec<Url> = Vec::new();
        if let CrawlResult::CrawlContent(v) = result {
            urls.append(&mut v.links.clone());
            urls.append(&mut v.extra_links.clone());
        }

        let mut found_urls: Vec<FoundURL> = vec![FoundURL::new(
            result.url().clone(),
            url_status,
            timestamp,
            None,
        )];
        for url in &urls {
            if let Some(domain) = url.domain()
                && !self.blacklist_provider.is_domain_blacklisted(domain).await
            {
                found_urls.push(FoundURL::new(url.clone(), UrlStatus::New, timestamp, None));
            }
        }

        db.update_found_urls(found_urls.iter_mut());

        let grouped = found_urls
            .clone()
            .into_iter()
            .into_group_map_by(|v| v.url().domain().map(std::borrow::ToOwned::to_owned));

        for (domain, urls) in grouped {
            if let Some(domain) = domain {
                self.add_domain_urls_later(&domain, &db, urls.clone());
            }
        }

        if let Some(domain) = result.url().domain() {
            db.update_domain_links(
                domain,
                found_urls
                    .into_iter()
                    .filter_map(|url| url.url().domain().map(std::borrow::ToOwned::to_owned)),
            );
        }
    }

    fn add_domain_urls_later(&self, domain: &str, db: &Arc<BloomDatabase>, urls: Vec<FoundURL>) {
        let domain = domain.to_owned();
        let domain_claims = self.domain_claims.clone();
        //let domain_urls = self.domain_urls.clone();
        let domain_scores = self.domain_scores.clone();
        let build_hasher = self.build_hasher.clone();
        let db = db.clone();

        let scores: Vec<(FoundURL, f32)> = urls
            .into_iter()
            .filter_map(|url| {
                let time_since_crawled = url.last_crawled().map_or(MAX_TIME_DELTA, |time| {
                    Duration::from_secs(
                        time.and_time(NaiveTime::default())
                            .signed_duration_since(Utc::now().naive_utc())
                            .abs()
                            .num_seconds()
                            .cast_unsigned(),
                    )
                });

                if time_since_crawled < Duration::from_hours(24 * 32) {
                    return None;
                }

                #[allow(clippy::cast_precision_loss)]
                let mut url_score = 1.0 / url.url().as_str().len() as f32;

                url_score *= 1.0
                    - (-time_since_crawled.as_secs_f32() / SCORE_TIME_CONSTANT.as_secs_f32()).exp();
                Some((url, url_score))
            })
            .collect();

        if scores.is_empty() {
            return;
        }
        let domain_score = db.get_domain_score(&domain);

        spawn(async move {
            let lock = match timeout(DOMAIN_TIMEOUT, domain_claims.entry_async(domain.clone()))
                .await
            {
                Ok(scc::hash_map::Entry::Occupied(occupied_entry)) => occupied_entry.get().clone(),
                Ok(scc::hash_map::Entry::Vacant(vacant_entry)) => {
                    let mutex = Arc::new(Mutex::new((
                        domain.clone(),
                        UrlQueue::new(),
                        Instant::now(),
                    )));
                    vacant_entry.insert_entry(mutex).get().clone()
                }
                Err(_) => {
                    return;
                }
            };
            let mut lock = ClaimedDomain::new(
                Arc::downgrade(&domain_claims),
                lock.lock_owned().await,
                true,
            );

            for (url, score) in scores {
                lock.domain_lock
                    .1
                    .push(url.url().clone(), OrderedFloat(score));
                if lock.domain_lock.1.len() > MAX_URL_PER_DOMAINS {
                    lock.domain_lock.1.pop_min();
                }
            }
            let domain_best = lock
                .domain_lock
                .1
                .peek_max()
                .map_or(OrderedFloat(-1.0), |v| *v.1);

            if let Some(domain_queue) =
                domain_scores.get(Self::get_priority_queue_index(&build_hasher, &domain))
            {
                let mut lock = domain_queue.write().await;
                lock.push(domain.clone(), domain_best + domain_score);
                if lock.len() > MAX_DOMAINS_PER_QUEUE
                    && let Some((evicted, _)) = lock.pop_min()
                {
                    drop(lock);
                    domain_claims.remove_async(&evicted).await;
                }
            }
            drop(lock);
        });
    }
}
