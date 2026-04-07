/// Wikipedia score lookup.
/// Replicates get_wiki_score from mwmbl/tinysearchengine/rank.py.

use std::collections::HashMap;
use once_cell::sync::Lazy;
use serde_json;

static WIKI_SCORES_JSON: &str = include_str!("../../mwmbl/resources/wiki_stats.json");

static WIKI_SCORES: Lazy<HashMap<String, f64>> = Lazy::new(|| {
    serde_json::from_str(WIKI_SCORES_JSON).expect("Failed to parse wiki_stats.json")
});

static WIKI_MAX_SCORE: Lazy<f64> = Lazy::new(|| {
    // The Python code uses `next(iter(WIKI_SCORES.values()))` which is the first value.
    // JSON object iteration order is insertion order in serde_json (via IndexMap if needed),
    // but the Python dict is ordered by insertion. We replicate by taking the max value,
    // which is equivalent since the file is sorted descending.
    WIKI_SCORES.values().cloned().fold(f64::NEG_INFINITY, f64::max)
});

/// Get a normalised wiki score for the given URL.
/// Extracts the last path segment as the Wikipedia article title.
pub fn get_wiki_score(url: &str) -> f64 {
    let title = url.split('/').next_back().unwrap_or("");
    if title.is_empty() {
        return 0.0;
    }
    let max = *WIKI_MAX_SCORE;
    if max <= 0.0 {
        return 0.0;
    }
    WIKI_SCORES.get(title).cloned().unwrap_or(0.0) / max
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wiki_score_unknown() {
        let score = get_wiki_score("https://en.wikipedia.org/wiki/Totally_Unknown_Article_XYZ");
        assert_eq!(score, 0.0);
    }

    #[test]
    fn test_wiki_score_range() {
        // Any known article should be in [0, 1]
        let score = get_wiki_score("https://en.wikipedia.org/wiki/Python_(programming_language)");
        assert!(score >= 0.0);
        assert!(score <= 1.0);
    }

    #[test]
    fn test_wiki_max_score_normalised() {
        // The top article should have score 1.0
        // Find the article with max score
        let max_title = WIKI_SCORES.iter()
            .max_by(|a, b| a.1.partial_cmp(b.1).unwrap())
            .map(|(k, _)| k.clone())
            .unwrap();
        let url = format!("https://en.wikipedia.org/wiki/{}", max_title);
        let score = get_wiki_score(&url);
        assert!((score - 1.0).abs() < 1e-9, "Max wiki score should be 1.0, got {}", score);
    }
}
