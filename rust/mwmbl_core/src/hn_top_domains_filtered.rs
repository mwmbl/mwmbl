use std::{collections::HashMap, sync::LazyLock};

use ordered_float::OrderedFloat;

/// The same table `mwmbl_rank` ranks with, so the two can't drift apart.
static DOMAINS_JSON: &str = include_str!("../data/hn_top_domains_filtered.json");

/// Domain to score, from the top Hacker News domains.
#[allow(clippy::expect_used)] // Checked in at build time and covered by a test, so it can't fail at run time.
pub static DOMAINS: LazyLock<HashMap<String, f64>> = LazyLock::new(|| {
    serde_json::from_str(DOMAINS_JSON).expect("hn_top_domains_filtered.json is valid JSON")
});

pub static DOMAIN_MIN_SCORE: LazyLock<f64> = LazyLock::new(|| {
    DOMAINS
        .values()
        .map(|v| OrderedFloat::from(*v))
        .min()
        .map_or(0.0, |v| *v)
});
#[allow(clippy::expect_used)] // Checked in at build time and covered by a test, so it can't fail at run time.
pub static DOMAIN_MAX_SCORE: LazyLock<f64> = LazyLock::new(|| {
    DOMAINS
        .values()
        .map(|v| OrderedFloat::from(*v))
        .max()
        .map_or(1.0, |v| *v)
});

#[cfg(test)]
mod tests {
    use super::DOMAINS;

    #[test]
    fn domains_parse() {
        assert!(DOMAINS.len() > 7000);
        assert!(DOMAINS.contains_key("paulgraham.com"));
    }
}
