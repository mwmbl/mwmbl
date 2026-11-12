use std::{collections::{BinaryHeap, HashSet, VecDeque}, hash::{BuildHasher, DefaultHasher, RandomState}, sync::{Arc, Weak}, time};

use mwmbl_core::{blacklist_providers::BlacklistProvider, constants::CORE_DOMAINS, hn_top_domains_filtered::DOMAINS};
use ordered_float::OrderedFloat;
use priority_queue::DoublePriorityQueue;
use rand::{RngExt, distr::StandardUniform, seq::IndexedRandom};
use scc::{HashCache, HashIndex, HashMap, queue};
use tokio::{sync::{Mutex, OwnedMutexGuard, RwLock}, task::yield_now};

use crate::{constants::{DOMAINS_VEC, NUM_OTHER_URLS_TO_INCLUDE, NUM_TOP_DOMAIN_URLS_TO_INCLUDE, P_SEED_DOMAIN}, curated_domains::CuratedDomains};


#[derive(Clone)]
pub struct SharedURLQueue<T: BlacklistProvider, const S: usize = 1000> {
    blacklist_provider: T,
    curated_domains: CuratedDomains,
    domain_claims: Arc<HashMap<String, Arc<Mutex<String>>>>,
    domain_urls: Arc<HashMap<String, DoublePriorityQueue<String, OrderedFloat<f32>>>>,
    domain_scores: Arc<[RwLock<DoublePriorityQueue<String, OrderedFloat<f32>>>; S]>,
    build_hasher: Arc<RandomState>,
}

pub struct ClaimedUrl {
    domain_lock: OwnedMutexGuard<String>,
    url: String,
    domain_claims: Weak<HashMap<String, Arc<Mutex<String>>>>
}

impl ClaimedUrl {
    pub fn url(&self) -> &str {
        &self.url
    }
    
    pub fn domain(&self) -> &str {
        &self.domain_lock
    }
}

impl Drop for ClaimedUrl {
    fn drop(&mut self) {
        let domain = self.domain_lock.clone();
        if let Some(claims) = self.domain_claims.upgrade(){
            claims.remove_sync(&domain);
        }
    }
}

impl<T: BlacklistProvider, const S: usize> SharedURLQueue<T,S> {
    pub fn new(
        blacklist_provider: T,
        curated_domains: CuratedDomains,
    ) -> SharedURLQueue<T, S> {
        SharedURLQueue {
            blacklist_provider,
            domain_urls: Arc::new(HashMap::default()),
            domain_claims: Arc::new(HashMap::default()),
            curated_domains,
            domain_scores: Arc::new(std::array::from_fn(|_| RwLock::new(DoublePriorityQueue::new()))),
            build_hasher: Arc::new(RandomState::new()),
        }
    }

    pub async fn claim_domain(&mut self, domain: &str) -> Result<OwnedMutexGuard<String>, tokio::sync::TryLockError> {
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
            p = rng.sample(StandardUniform);

            if choose_seed_domain {
                // SELECT A SEED DOMAIN
                let curated_non_static_domains = self.curated_domains.get_curated_non_static_domains();
                let random_domain = if p < (DOMAINS_VEC.len() as f32)/((DOMAINS_VEC.len() + curated_non_static_domains.read().await.len()) as f32) {
                    DOMAINS_VEC.sample(&mut rng, 1).next().cloned()
                } else {
                    curated_non_static_domains.read().await.sample(&mut rng, 1).next().cloned()
                };
                if let Some(random_domain) = random_domain && !self.blacklist_provider.is_domain_blacklisted(&random_domain).await {
                    if let Ok(domain_lock) = self.claim_domain(&random_domain).await {
                        return ClaimedUrl {
                            domain_lock, 
                            url: format!("https://{random_domain}/"),
                            domain_claims: Arc::downgrade(&self.domain_claims) 
                        };
                    }
                }
            } else if p < ((NUM_OTHER_URLS_TO_INCLUDE+NUM_TOP_DOMAIN_URLS_TO_INCLUDE) as f32)/total {
                // SELECT EITHER A TOP_CURATED_DOMAIN OR A TOP_DOMAIN NON CURATED
                let select_from_curated = p >= (NUM_OTHER_URLS_TO_INCLUDE as f32)/total;
                let samples = self.domain_scores.sample(&mut rng, S);
                let mut domain: Option<String> = None;
                for queue in samples {
                    let mut queue  = queue.write().await;
                    let top_domain = if let Some((top_domain, _)) = queue.peek_max() {
                        top_domain.to_owned()
                    } else {
                        continue;
                    };
                    if self.blacklist_provider.is_domain_blacklisted(&top_domain).await {
                        queue.pop_max();
                        self.domain_urls.remove_async(&top_domain).await;
                        continue;
                    }
                    if (DOMAINS.contains_key(&top_domain) || self.curated_domains.is_domain_curated(&top_domain).await) == select_from_curated {
                        domain = Some(top_domain);
                        break;
                    }
                }
                if let Some(domain) = domain {
                    if let Some(claimed_url) = self.claim_url(&domain).await {
                        return claimed_url;
                    }
                }
            } else {
                // SELECT A CORE_DOMAINS
                let domain = CORE_DOMAINS.sample(&mut rng, 1).next().cloned();
                if let Some(domain) = domain && !self.blacklist_provider.is_domain_blacklisted(&domain).await {
                    if let Some(claimed_url) = self.claim_url(&domain).await {
                        return claimed_url;
                    }
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
        if let Ok(domain_lock) = self.claim_domain(&domain).await {
            if let Some(mut urls) = self.domain_urls.get_async(domain).await {
                if let Some((url, _)) = urls.pop_max() {

                    if let Some((_, priority)) = urls.peek_max() {
                        if let Some(domain_queue) = self.get_priority_queue(domain).await  {
                            domain_queue.write().await.push(domain.to_owned(), priority.clone());
                        }
                    } else {
                        let _ = urls.remove();
                        if let Some(domain_queue) = self.get_priority_queue(domain).await {
                            domain_queue.write().await.remove(domain);
                        }
                    }

                    return Some(ClaimedUrl {
                        domain_lock, 
                        url: url,
                        domain_claims: Arc::downgrade(&self.domain_claims)
                    });
                } else {
                    let _ = urls.remove();
                    if let Some(queue) = self.get_priority_queue(domain).await {
                        queue.write().await.remove(domain);
                    }
                }
            }
        }
        None
    }

    async fn get_priority_queue(&self, domain: &str) -> Option<&RwLock<DoublePriorityQueue<String, OrderedFloat<f32>>>> {
        self.domain_scores.get(self.build_hasher.hash_one(domain) as usize%S)
    }
}
