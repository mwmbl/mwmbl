use std::{collections::HashMap, sync::LazyLock};

/// The same table `mwmbl_rank` ranks with, so the two can't drift apart.
static DOMAINS_JSON: &str = include_str!("../../../mwmbl_rank/data/hn_top_domains_filtered.json");

/// Domain to score, from the top Hacker News domains.
#[allow(clippy::expect_used)] // Checked in at build time and covered by a test, so it can't fail at run time.
pub static DOMAINS: LazyLock<HashMap<String, f64>> = LazyLock::new(|| {
    serde_json::from_str(DOMAINS_JSON).expect("hn_top_domains_filtered.json is valid JSON")
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
