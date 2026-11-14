use std::{sync::LazyLock, time::Duration};

use mwmbl_core::hn_top_domains_filtered::DOMAINS;

pub static INDEX_NAME: &str = "crawl-index.tinysearch";
pub static NUM_PAGES: usize = 100_000;
pub static DATA_DIR: &str = ".mwmbl";

pub static PROJECT_URL: &str = "https://github.com/mwmbl/mwmbl";
pub static API_PRODUCT: &str = concat!("mwmbl-crawler/", env!("CARGO_PKG_VERSION"));

pub static TOP_CURATED_DOMAIN_RANDOM_WEIGHT: usize = 50;
pub static TOP_OTHER_DOMAIN_RANDOM_WEIGHT: usize = 100;
pub static NUM_PRIOTITY_QUEUES: usize = 1000;
pub static MAX_OTHER_DOMAINS: usize = 10000;

pub static SEED_DOMAIN_RANDOM_WEIGHT: usize = 1;
pub static KNOWN_DOMAINS_RANDOM_WEIGHT: usize = 99;

pub static MAX_REDIRECTS: usize = 5;
pub static TIMEOUT: Duration = Duration::from_secs(3);
pub static MAX_FETCH_SIZE: usize = 1024 * 1024;

pub static DOMAINS_VEC: LazyLock<Vec<String>> = LazyLock::new(|| DOMAINS.keys().cloned().collect());

pub static ROBOTS_CACHE_TTL_SECONDS: Duration = Duration::from_hours(24);
pub static ROBOTS_CACHE_ERROR_TTL_SECONDS: Duration = Duration::from_hours(1);
