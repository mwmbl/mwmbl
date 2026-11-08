use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, RwLock},
};

use mwmbl_core::{
    blacklist_providers::{
        BlacklistProvider, BuiltInRulesBlacklistProvider, CuratedBlacklistProvider,
        SnapshotBlacklist,
    },
    curated_domains::SharedCuratedDomains,
};

use crate::{CrawlerParams, redis_url_queue::RedisURLQueue};

pub fn fetch_curated_domains() -> HashSet<String> {
    todo!()
}

pub struct Crawler<
    T: BlacklistProvider = SnapshotBlacklist,
    T2: BlacklistProvider = CuratedBlacklistProvider,
> {
    redis: redis::Client,
    url_queue: RedisURLQueue<T2>,
    blacklist: T,
    username: Arc<RwLock<Option<String>>>,
    error_last_reported_at: Arc<RwLock<HashMap<String, f32>>>,
    params: CrawlerParams,
}

impl Crawler {
    pub fn new(params: CrawlerParams) -> anyhow::Result<Crawler> {
        let redis = redis::Client::open(params.redis_url.clone())?;
        let built_in_rules = BuiltInRulesBlacklistProvider::new();
        let blacklist = SnapshotBlacklist::new(&redis, built_in_rules);
        let curated_domains = SharedCuratedDomains::new();
        let curated_blacklist = CuratedBlacklistProvider::new(blacklist.clone(), curated_domains);
        let url_queue = RedisURLQueue::new(&redis, curated_blacklist);

        Ok(Crawler {
            redis,
            url_queue,
            blacklist,
            username: Arc::new(RwLock::new(None)),
            error_last_reported_at: Arc::new(RwLock::new(HashMap::default())),
            params,
        })
    }

    pub fn run(&self) -> anyhow::Result<()> {
        self.check_redis()?;

        todo!();
    }

    pub fn run_once(&self) -> anyhow::Result<()> {
        self.check_redis()?;

        todo!();
    }

    pub fn check_redis(&self) -> anyhow::Result<()> {
        todo!();
    }
}
