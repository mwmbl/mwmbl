use std::{collections::{BinaryHeap, HashSet, VecDeque}, sync::Arc, time};

use mwmbl_core::{blacklist_providers::BlacklistProvider, constants::CORE_DOMAINS};
use rand::{RngExt, distr::StandardUniform, seq::IndexedRandom};
use scc::{HashCache, HashIndex, HashMap};
use tokio::{sync::{Mutex, OwnedMutexGuard, RwLock}, task::yield_now};

use crate::{constants::{DOMAINS_VEC, NUM_OTHER_URLS_TO_INCLUDE, NUM_TOP_DOMAIN_URLS_TO_INCLUDE, P_SEED_DOMAIN}, curated_domains::CuratedDomains};

pub trait URLQueue: Clone + Send + Sync {}


#[derive(Clone)]
pub struct SharedURLQueue<T: BlacklistProvider> {
    blacklist_provider: T,
    queue: Arc<RwLock<VecDeque<String>>>,
    domain_claims: Arc<HashMap<String, Arc<Mutex<String>>>>,
    // domain_scores: Arc<RwLock<BinaryHeap<String>>>,
    curated_domains: CuratedDomains,
}


pub struct ClaimedUrl {
    domain_lock: OwnedMutexGuard<String>,
    url: String
}

impl ClaimedUrl {
    pub fn url(&self) -> &str {
        &self.url
    }
}

impl<T: BlacklistProvider> URLQueue for SharedURLQueue<T> {}

impl<T: BlacklistProvider> SharedURLQueue<T> {
    pub fn new(
        blacklist_provider: T,
        curated_domains: CuratedDomains,
    ) -> SharedURLQueue<T> {
        let queue: VecDeque<String> = VecDeque::default();
        let domain_locks: HashMap<String, Arc<Mutex<String>>> = HashMap::default();
        SharedURLQueue {
            blacklist_provider,
            queue: Arc::new(RwLock::new(queue)),
            domain_claims: Arc::new(domain_locks),
            curated_domains,
            // domain_scores: todo!(),
        }
    }

    pub async fn claim(&mut self, domain: &str) -> Result<OwnedMutexGuard<String>, tokio::sync::TryLockError> {
        let entry = self.domain_claims.entry_async(domain.to_owned()).await;
        match entry {
            scc::hash_map::Entry::Occupied(occupied_entry) => {
                let mutex = occupied_entry.get().clone();
                mutex.try_lock_owned()
            },
            scc::hash_map::Entry::Vacant(vacant_entry) => {
                let mutex = Arc::new(Mutex::new(domain.to_owned()));
                vacant_entry.insert_entry(mutex).get().clone().try_lock_owned()
            },
        }
    }

    pub async fn get_url(&mut self) -> ClaimedUrl {
        let mut rng = rand::rng();

        loop {
            // loop until we can claim a domain, if claim fails we yield to allow other computation to add new domains
            let mut p:f32 = rng.sample(StandardUniform);
            let choose_seed_domain = p < P_SEED_DOMAIN;
            let total: f32 = (NUM_OTHER_URLS_TO_INCLUDE+NUM_TOP_DOMAIN_URLS_TO_INCLUDE+CORE_DOMAINS.len()) as f32;
            p = (p-P_SEED_DOMAIN)/(1.0-P_SEED_DOMAIN);

            if choose_seed_domain {
                // SELECT A SEED DOMAIN
                let curated_non_static_domains = self.curated_domains.get_curated_non_static_domains();
                let random_domain = if p < (DOMAINS_VEC.len() as f32)/(DOMAINS_VEC.len() as f32 + curated_non_static_domains.read().await.len() as f32) {
                    DOMAINS_VEC.sample(&mut rng, 1).next().cloned()
                } else {
                    curated_non_static_domains.read().await.sample(&mut rng, 1).next().cloned()
                };
                if let Some(random_domain) = random_domain && let Ok(false) = self.blacklist_provider.is_domain_blacklisted(&random_domain).await {
                    if let Ok(domain_lock) = self.claim(&random_domain).await {
                        return ClaimedUrl {domain_lock, url: format!("https://{random_domain}/") };
                    }
                }
            } else if p < (NUM_OTHER_URLS_TO_INCLUDE as f32)/total {
                // TODO: SELECT A OTHER DOMAIN
            } else if p < ((NUM_OTHER_URLS_TO_INCLUDE+NUM_TOP_DOMAIN_URLS_TO_INCLUDE) as f32)/total {
                // TODO: SELECT A TOP_DOMAIN
            } else {
                // TODO: SELECT A CORE_DOMAINS
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
}
