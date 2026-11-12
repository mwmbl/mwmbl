use std::sync::LazyLock;

use mwmbl_core::hn_top_domains_filtered::DOMAINS;

pub static INDEX_NAME: &str = "crawl-index.tinysearch";
pub static NUM_PAGES: usize = 100_000;
pub static DATA_DIR: &str = ".mwmbl";

pub static PROJECT_URL: &str = "https://github.com/mwmbl/mwmbl";
pub static API_PRODUCT: &str = concat!("mwmbl-crawler/", env!("CARGO_PKG_VERSION"));

pub static NUM_TOP_DOMAIN_URLS_TO_INCLUDE: usize = 50;
pub static NUM_OTHER_URLS_TO_INCLUDE: usize = 100;
pub static NUM_PRIOTITY_QUEUES: usize = 1000;
pub static P_SEED_DOMAIN: f32 = 0.01;

pub static MAX_REDIRECTS: usize = 5;
pub static TIMEOUT_SECONDS: usize = 3;

pub static DOMAINS_VEC: LazyLock<Vec<String>> = LazyLock::new(|| {
    DOMAINS.iter().map(|(k, _)| k.clone()).collect()
});