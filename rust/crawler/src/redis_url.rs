use mwmbl_core::blacklist_providers::BlacklistProvider;

pub trait URLQueue: Clone + Send + Sync {}

#[derive(Clone)]
pub struct SharedURLQueue<T: BlacklistProvider> {
    blacklist_provider: T,
}

impl<T: BlacklistProvider> URLQueue for SharedURLQueue<T> {}

impl<T: BlacklistProvider> SharedURLQueue<T> {
    pub fn new(blacklist_provider: T) -> SharedURLQueue<T> {
        SharedURLQueue { blacklist_provider }
    }
}

// pub struct RedisURLQueue<T: BlacklistProvider> {
//     redis: redis::Client,
//     blacklist_provider: T,
// }

// impl<T: BlacklistProvider> RedisURLQueue<T> {
//     pub fn new(redis: &redis::Client, blacklist_provider: T) -> RedisURLQueue<T> {
//         RedisURLQueue {
//             redis: redis.clone(),
//             blacklist_provider,
//         }
//     }
// }

// impl<T: BlacklistProvider> URLQueue for RedisURLQueue<T> {}
