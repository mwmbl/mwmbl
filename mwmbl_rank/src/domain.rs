/// Domain scoring using the HN top domains list.
/// The DOMAINS data is loaded from a JSON file embedded at compile time.
/// Replicates get_domain_score from mwmbl/tinysearchengine/rank.py.

use std::collections::HashMap;
use once_cell::sync::Lazy;
use serde_json;
use url::Url;

static DOMAINS_JSON: &str = include_str!("../data/hn_top_domains_filtered.json");

static DOMAINS: Lazy<HashMap<String, f64>> = Lazy::new(|| {
    serde_json::from_str(DOMAINS_JSON).expect("Failed to parse hn_top_domains_filtered.json")
});

static DOMAIN_MAX_SCORE: Lazy<f64> = Lazy::new(|| {
    DOMAINS.values().cloned().fold(f64::NEG_INFINITY, f64::max)
});

static DOMAIN_MIN_SCORE: Lazy<f64> = Lazy::new(|| {
    DOMAINS.values().cloned().fold(f64::INFINITY, f64::min)
});

/// Extract the netloc (host) from a URL string.
pub fn get_netloc(url: &str) -> String {
    Url::parse(url)
        .ok()
        .and_then(|u| u.host_str().map(|h| h.to_string()))
        .unwrap_or_default()
}

/// Get a normalised domain score in [0, 1] for the given URL.
/// Returns 0.0 if the domain is not in the HN top domains list.
pub fn get_domain_score(url: &str) -> f64 {
    let domain = get_netloc(url);
    if domain.is_empty() {
        return 0.0;
    }

    if let Some(&score) = DOMAINS.get(&domain) {
        let max = *DOMAIN_MAX_SCORE;
        let min = *DOMAIN_MIN_SCORE;
        if (max - min).abs() < f64::EPSILON {
            return 0.0;
        }
        (score - min) / (max - min)
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_get_netloc() {
        assert_eq!(get_netloc("https://paulgraham.com/articles.html"), "paulgraham.com");
        assert_eq!(get_netloc("https://blog.rust-lang.org/"), "blog.rust-lang.org");
        assert_eq!(get_netloc("not-a-url"), "");
    }

    #[test]
    fn test_domain_score_known() {
        // paulgraham.com is in the top domains list
        let score = get_domain_score("https://paulgraham.com/articles.html");
        assert!(score > 0.0, "Expected positive score for known domain");
        assert!(score <= 1.0, "Score should be <= 1.0");
    }

    #[test]
    fn test_domain_score_unknown() {
        let score = get_domain_score("https://totally-unknown-domain-xyz123.example.com/");
        assert_eq!(score, 0.0);
    }

    #[test]
    fn test_domain_score_top_domain_near_one() {
        // The top domain should have score close to 1.0
        let score = get_domain_score("https://blog.samaltman.com/");
        assert!(score > 0.99, "Top domain should have score near 1.0, got {}", score);
    }
}
