use std::{sync::LazyLock, time::Duration};

use mwmbl_core::hn_top_domains_filtered::DOMAINS;
use ordered_float::OrderedFloat;

pub static INDEX_NAME: &str = "crawl-index.tinysearch";
pub static BLOOM_NAME: &str = "crawl-bloom";
pub static NUM_PAGES: usize = 100_000;
pub static DATA_DIR: &str = ".mwmbl";

pub static PROJECT_URL: &str = "https://github.com/mwmbl/mwmbl";
pub static API_PRODUCT: &str = concat!("mwmbl-crawler/", env!("CARGO_PKG_VERSION"));
// What robots.txt groups are matched against.
pub static ROBOTS_USER_AGENT: &str = "mwmbl-crawler";

pub static TOP_CURATED_DOMAIN_RANDOM_WEIGHT: usize = 50;
pub static TOP_OTHER_DOMAIN_RANDOM_WEIGHT: usize = 100;
pub static MAX_DOMAINS_PER_QUEUE: usize = 20;
pub static MAX_URL_PER_DOMAINS: usize = 100;

pub static SEED_DOMAIN_RANDOM_WEIGHT: usize = 1;
pub static KNOWN_DOMAINS_RANDOM_WEIGHT: usize = 99;

pub static MAX_REDIRECTS: usize = 5;
pub static TIMEOUT: Duration = Duration::from_secs(30);
pub static MAX_FETCH_SIZE: usize = 1024 * 1024;

pub static DOMAINS_VEC: LazyLock<Vec<String>> = LazyLock::new(|| {
    let mut v: Vec<_> = DOMAINS.iter().collect();
    v.sort_by_key(|(_, v)| OrderedFloat::from(**v));
    v.into_iter().map(|(k, _)| k.to_owned()).collect()
});

pub static ROBOTS_CACHE_TTL_SECONDS: Duration = Duration::from_hours(24);
pub static ROBOTS_CACHE_ERROR_TTL_SECONDS: Duration = Duration::from_hours(1);

pub static NUM_EXTRACT_CHARS: usize = 155;
pub static NUM_TITLE_CHARS: usize = 65;

pub static DEFAULT_DOMAIN_DELAY: Duration = Duration::from_secs(5);
pub static MAX_BLOCK_COOL_OFF: Duration = Duration::from_hours(6);
pub static MAX_DOMAIN_DELAY: Duration = MAX_BLOCK_COOL_OFF;

pub static BLOCK_COOL_OFF: Duration = Duration::from_mins(1);

// much bigger than python to because we keep a single of it
pub static NUM_DOMAINS_IN_BLOOM_FILTER: usize = 10_000_000;

pub static LINK_DOMAIN_DB_SAVE_INTERVAL: Duration = Duration::from_mins(5);
pub static URL_DB_SAVE_INTERVAL: Duration = Duration::from_mins(5);

pub static MAX_TIME_DELTA: Duration = Duration::from_hours(24 * 100_000);
pub static SCORE_TIME_CONSTANT: Duration = Duration::from_hours(24 * 30 * 10);
