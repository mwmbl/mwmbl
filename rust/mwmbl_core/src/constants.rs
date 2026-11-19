use std::{sync::LazyLock, time::Duration};

use phf::phf_set;
use regex::Regex;

// to avoid some rust analyzer bugs
// pub static PAGE_SIZE: usize = 4096;
macro_rules! PAGE_SIZE {
    // `()` indicates that the macro takes no argument.
    () => {
        4096 //
    };
}
pub(crate) use PAGE_SIZE;

// to avoid some rust analyzer bugs
// pub static METADATA_SIZE: usize = 4096;
macro_rules! METADATA_SIZE {
    // `()` indicates that the macro takes no argument.
    () => {
        4096 //
    };
}
pub(crate) use METADATA_SIZE;

pub static INDEX_VERSION: i32 = 1;
pub static METADATA_CONSTANT: &[u8; 17] = b"mwmbl-tiny-search";
pub static ITEM_FACTORY: &str = "Document";

pub static BLACKLIST_REFRESH_CHECK: Duration = Duration::from_mins(5);

pub static EXCLUDED_DOMAINS: phf::Set<&'static str> = phf_set! {
    "web.archive.org",
    "forums.giantitp.com",
    "www.crutchfield.com",
    "plus.google.com",
    "www.lukas-renggli.ch",
    // Adult content sites found ranking for suspicious Search Console queries (2026-08-14).
    "fineartteens.com",
    "milforia.com",
    "azgals.com",
    "kusowanka.com",
    // The site owner asked to be removed from the index (issue #559).
    "lain.ovh",
};

pub static CORE_DOMAINS: [&str; 9] = [
    "github.com",
    "en.wikipedia.org",
    "stackoverflow.com",
    "docs.google.com",
    "programmers.stackexchange.com",
    "developer.mozilla.org",
    "arxiv.org",
    "www.python.org",
    "news.ycombinator.com",
];

#[allow(clippy::unwrap_used)]
pub static DOMAIN_BLACKLIST_REGEX: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"porn|xxx|adult|hentai|\.booru\.org$|jksu\.org|lwhyl\.org$|rgcd\.cn$|hzqwyou\.cn$|omgoat\.org$|pussyboy\.net$").unwrap()
});
#[allow(clippy::unwrap_used)]
pub static NUMBERS_ONLY_REGEX: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9]+$").unwrap());

pub static NUM_FIRST_TOKENS: usize = 10;
pub static DOCUMENT_STATE_CURATED_THRESHOLD: u8 = 7;

pub static MATCH_EXPONENT: f32 = 2.0;
