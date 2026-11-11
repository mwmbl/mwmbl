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