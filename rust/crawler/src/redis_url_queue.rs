use mwmbl_core::blacklist_providers::{BlacklistProvider, CuratedBlacklistProvider};

pub struct RedisURLQueue<T: BlacklistProvider = CuratedBlacklistProvider> {
    redis: redis::Client,
    blacklist_provider: T
}

impl<T: BlacklistProvider> RedisURLQueue<T> {
    pub fn new(redis: &redis::Client, blacklist_provider: T) -> RedisURLQueue<T> {
        RedisURLQueue {
            redis: redis.clone(),
            blacklist_provider
        }
    }
    
}