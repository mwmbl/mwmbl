/// Feature extraction for learning-to-rank.
/// Ports get_features, get_match_features, and score_match from
/// mwmbl/tinysearchengine/rank.py.

use std::collections::HashMap;
use url::Url;
use regex::Regex;

use crate::text::build_query_regex;
use crate::domain::get_domain_score;
use crate::wiki::get_wiki_score;

const MATCH_EXPONENT: f64 = 2.0;

/// The canonical feature names in the order they appear in the feature vector.
/// This order MUST match what the Python FeatureExtractor produces so that
/// models trained in Rust are compatible with models trained in Python.
pub const FEATURE_NAMES: &[&str] = &[
    // title
    "last_match_char_title",
    "match_length_title",
    "total_possible_match_length_title",
    "match_score_title",
    "match_terms_title",
    "match_term_proportion_title",
    // extract
    "last_match_char_extract",
    "match_length_extract",
    "total_possible_match_length_extract",
    "match_score_extract",
    "match_terms_extract",
    "match_term_proportion_extract",
    // domain
    "last_match_char_domain",
    "match_length_domain",
    "total_possible_match_length_domain",
    "match_score_domain",
    "match_terms_domain",
    "match_term_proportion_domain",
    // domain_tokenized
    "last_match_char_domain_tokenized",
    "match_length_domain_tokenized",
    "total_possible_match_length_domain_tokenized",
    "match_score_domain_tokenized",
    "match_terms_domain_tokenized",
    "match_term_proportion_domain_tokenized",
    // path
    "last_match_char_path",
    "match_length_path",
    "total_possible_match_length_path",
    "match_score_path",
    "match_terms_path",
    "match_term_proportion_path",
    // query (URL query string)
    "last_match_char_query",
    "match_length_query",
    "total_possible_match_length_query",
    "match_score_query",
    "match_terms_query",
    "match_term_proportion_query",
    // whole
    "last_match_char_whole",
    "match_length_whole",
    "total_possible_match_length_whole",
    "match_score_whole",
    "match_terms_whole",
    "match_term_proportion_whole",
    // scalar features
    "num_terms",
    "num_chars",
    "domain_score",
    "path_length",
    "domain_length",
    "wiki_score",
    "item_score",
    "match_terms",
];

pub const NUM_FEATURES: usize = FEATURE_NAMES.len(); // 50

/// Result of matching query terms against a text part.
#[derive(Debug, Default)]
pub struct MatchFeatures {
    pub last_match_char: usize,
    pub match_length: usize,
    pub total_possible_match_length: usize,
    pub match_terms: usize,
    pub match_counts: HashMap<String, usize>,
}

/// Compute the match score from raw match statistics.
/// Replicates score_match from rank.py:
///   MATCH_EXPONENT ** (match_length - total_possible_match_length) / last_match_char
pub fn score_match(last_match_char: usize, match_length: usize, total_possible: usize) -> f64 {
    let lmc = last_match_char.max(1) as f64; // guard against division by zero
    MATCH_EXPONENT.powi(match_length as i32 - total_possible as i32) / lmc
}

/// Compute match features for a single text part against the query terms.
/// Replicates get_match_features from rank.py.
///
/// - `terms`: query terms (already lowercased)
/// - `result_string`: the text to search in
/// - `is_complete`: whether the query is complete (trailing space)
/// - `is_url`: whether to use word boundaries
pub fn get_match_features(
    terms: &[&str],
    result_string: &str,
    is_complete: bool,
    is_url: bool,
) -> MatchFeatures {
    let total_possible_match_length: usize = terms.iter().map(|t| t.len()).sum();

    let re = match build_query_regex(terms, is_complete, is_url) {
        Some(r) => r,
        None => {
            return MatchFeatures {
                last_match_char: 1,
                match_length: 0,
                total_possible_match_length,
                match_terms: 0,
                match_counts: HashMap::new(),
            };
        }
    };
    get_match_features_with_regex(total_possible_match_length, result_string, &re)
}

/// Inner implementation that accepts a pre-compiled regex.
/// Used by get_features to avoid recompiling the same regex for each text part.
fn get_match_features_with_regex(
    total_possible_match_length: usize,
    result_string: &str,
    re: &Regex,
) -> MatchFeatures {

    let mut last_match_char: usize = 1;
    let mut match_length: usize = 0;
    let mut seen_matches: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut match_counts: HashMap<String, usize> = HashMap::new();

    for m in re.find_iter(result_string) {
        let value = m.as_str().to_lowercase();
        *match_counts.entry(value.clone()).or_insert(0) += 1;
        if !seen_matches.contains(&value) {
            last_match_char = m.end();
            seen_matches.insert(value);
            match_length += m.len();
        }
    }

    // If no matches found, last_match_char stays 1 (matches Python behaviour)
    if seen_matches.is_empty() {
        last_match_char = 1;
    }

    MatchFeatures {
        last_match_char,
        match_length,
        total_possible_match_length,
        match_terms: seen_matches.len(),
        match_counts,
    }
}

/// Parse a URL into its components.
struct ParsedUrl {
    domain: String,
    path: String,
    query: String,
}

fn parse_url(url: &str) -> ParsedUrl {
    match Url::parse(url) {
        Ok(u) => ParsedUrl {
            domain: u.host_str().unwrap_or("").to_string(),
            path: u.path().to_string(),
            query: u.query().unwrap_or("").to_string(),
        },
        Err(_) => ParsedUrl {
            domain: String::new(),
            path: String::new(),
            query: String::new(),
        },
    }
}

/// Append 6 zero-valued match features when there are no query terms (empty regex).
fn push_zero_part_features(features: &mut Vec<f32>, total_possible_match_length: usize) {
    let ms = score_match(1, 0, total_possible_match_length);
    features.push(1.0_f32);                          // last_match_char
    features.push(0.0_f32);                          // match_length
    features.push(total_possible_match_length as f32); // total_possible_match_length
    features.push(ms as f32);                        // match_score
    features.push(0.0_f32);                          // match_terms
    features.push(0.0_f32);                          // match_term_proportion
}

/// Append 6 match-related features for one text part to the feature vector.
/// Accepts a pre-compiled regex to avoid recompilation per text part.
fn push_part_features_with_regex(
    features: &mut Vec<f32>,
    total_possible_match_length: usize,
    text: &str,
    re: &Regex,
    num_terms: usize,
) {
    let mf = get_match_features_with_regex(total_possible_match_length, text, re);
    let ms = score_match(mf.last_match_char, mf.match_length, mf.total_possible_match_length);
    let denom = num_terms.max(1) as f64;

    features.push(mf.last_match_char as f32);
    features.push(mf.match_length as f32);
    features.push(mf.total_possible_match_length as f32);
    features.push(ms as f32);
    features.push(mf.match_terms as f32);
    features.push((mf.match_terms as f64 / denom) as f32);
}

/// Append 6 match-related features for one text part to the feature vector.
/// Compiles the regex internally — use push_part_features_with_regex when possible.
fn push_part_features(
    features: &mut Vec<f32>,
    terms: &[&str],
    text: &str,
    is_complete: bool,
    is_url: bool,
) {
    let mf = get_match_features(terms, text, is_complete, is_url);
    let ms = score_match(mf.last_match_char, mf.match_length, mf.total_possible_match_length);
    let num_terms = terms.len().max(1) as f64;

    features.push(mf.last_match_char as f32);
    features.push(mf.match_length as f32);
    features.push(mf.total_possible_match_length as f32);
    features.push(ms as f32);
    features.push(mf.match_terms as f32);
    features.push((mf.match_terms as f64 / num_terms) as f32);
}

/// Compute the full feature vector for a single (query, document) pair.
/// Returns a Vec<f32> of length NUM_FEATURES (50) in the canonical order
/// defined by FEATURE_NAMES.
///
/// Replicates get_features from mwmbl/tinysearchengine/rank.py.
///
/// Compiles each regex only once per call (2 regexes: url-boundary and non-url-boundary),
/// then reuses them across all 7 text parts.
pub fn get_features(
    terms: &[&str],
    title: &str,
    url: &str,
    extract: &str,
    score: f32,
    is_complete: bool,
) -> Vec<f32> {
    // Compile each regex variant exactly once per get_features call.
    // re_text: case-insensitive, no word boundaries (for title, extract, domain_tokenized, query, whole)
    // re_url:  case-insensitive, with \b word boundaries (for domain, path)
    let re_text = build_query_regex(terms, is_complete, false);
    let re_url  = build_query_regex(terms, is_complete, true);
    get_features_with_regex(terms, title, url, extract, score, re_text.as_ref(), re_url.as_ref())
}

/// Like `get_features` but accepts pre-compiled regexes to avoid recompilation
/// when processing many records that share the same query.
///
/// `re_text`: regex for non-URL text parts (title, extract, domain_tokenized, query, whole)
/// `re_url`:  regex for URL parts with word boundaries (domain, path)
pub fn get_features_with_regex(
    terms: &[&str],
    title: &str,
    url: &str,
    extract: &str,
    score: f32,
    re_text: Option<&Regex>,
    re_url: Option<&Regex>,
) -> Vec<f32> {
    let parsed = parse_url(url);
    let domain = &parsed.domain;
    let path = &parsed.path;
    let query_str = &parsed.query;

    let whole = format!("{} {} {} {} {}", title, extract, domain, path, query_str);

    let total_possible_match_length: usize = terms.iter().map(|t| t.len()).sum();
    let num_terms = terms.len();

    let mut features: Vec<f32> = Vec::with_capacity(NUM_FEATURES);

    // 7 text parts × 6 features each = 42 features
    // (title, extract, domain[url], domain[tokenized], path[url], query, whole)
    macro_rules! push_text {
        ($text:expr) => {
            match re_text {
                Some(re) => push_part_features_with_regex(&mut features, total_possible_match_length, $text, re, num_terms),
                None => push_zero_part_features(&mut features, total_possible_match_length),
            }
        };
    }
    macro_rules! push_url {
        ($text:expr) => {
            match re_url {
                Some(re) => push_part_features_with_regex(&mut features, total_possible_match_length, $text, re, num_terms),
                None => push_zero_part_features(&mut features, total_possible_match_length),
            }
        };
    }

    push_text!(title);
    push_text!(extract);
    push_url!(domain);    // domain as URL (word boundaries)
    push_text!(domain);   // domain tokenized (no word boundaries)
    push_url!(path);      // path as URL (word boundaries)
    push_text!(query_str);
    push_text!(&whole);

    // 8 scalar features
    let num_terms_f = terms.len() as f32;
    let num_chars = terms.iter().map(|t| t.len()).sum::<usize>() as f32
        + (terms.len().saturating_sub(1)) as f32; // spaces between terms

    // match_terms = max over title, extract, domain, domain_tokenized, path
    // These are at indices: 4, 10, 16, 22, 28 (match_terms_* for each part)
    let match_terms_max = [features[4], features[10], features[16], features[22], features[28]]
        .iter()
        .cloned()
        .fold(f32::NEG_INFINITY, f32::max);

    features.push(num_terms_f);
    features.push(num_chars);
    features.push(get_domain_score(url) as f32);
    features.push(path.len() as f32);
    features.push(domain.len() as f32);
    features.push(get_wiki_score(url) as f32);
    features.push(score);
    features.push(match_terms_max);

    debug_assert_eq!(
        features.len(),
        NUM_FEATURES,
        "Feature vector length mismatch: expected {}, got {}",
        NUM_FEATURES,
        features.len()
    );

    features
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_feature_count() {
        assert_eq!(FEATURE_NAMES.len(), NUM_FEATURES);
        assert_eq!(NUM_FEATURES, 50);
    }

    #[test]
    fn test_get_features_length() {
        let terms = vec!["rust", "programming"];
        let features = get_features(
            &terms,
            "Rust Programming Language",
            "https://www.rust-lang.org/",
            "A systems programming language",
            1.0,
            true,
        );
        assert_eq!(features.len(), NUM_FEATURES);
    }

    #[test]
    fn test_score_match_basic() {
        // When match_length == total_possible, exponent is 0, result is 1/last_match_char
        let score = score_match(5, 4, 4);
        assert!((score - 1.0 / 5.0).abs() < 1e-9);
    }

    #[test]
    fn test_score_match_no_match() {
        // match_length = 0, total_possible = 4 → 2^(0-4) / 1 = 1/16
        let score = score_match(1, 0, 4);
        assert!((score - 1.0 / 16.0).abs() < 1e-9);
    }

    #[test]
    fn test_get_match_features_basic() {
        let terms = vec!["rust"];
        let mf = get_match_features(&terms, "I love Rust programming", true, false);
        assert_eq!(mf.match_terms, 1);
        assert!(mf.match_length > 0);
        assert_eq!(mf.total_possible_match_length, 4); // "rust" has 4 chars
    }

    #[test]
    fn test_get_match_features_no_match() {
        let terms = vec!["python"];
        let mf = get_match_features(&terms, "I love Rust programming", true, false);
        assert_eq!(mf.match_terms, 0);
        assert_eq!(mf.match_length, 0);
        assert_eq!(mf.last_match_char, 1);
    }

    #[test]
    fn test_get_match_features_multiple_terms() {
        let terms = vec!["rust", "programming"];
        let mf = get_match_features(&terms, "Rust programming language", true, false);
        assert_eq!(mf.match_terms, 2);
    }

    #[test]
    fn test_features_no_nan() {
        let terms = vec!["test"];
        let features = get_features(
            &terms,
            "",
            "https://example.com/",
            "",
            0.0,
            true,
        );
        for (i, &f) in features.iter().enumerate() {
            assert!(!f.is_nan(), "Feature {} ({}) is NaN", i, FEATURE_NAMES[i]);
        }
    }

    #[test]
    fn test_features_empty_terms() {
        let terms: Vec<&str> = vec![];
        let features = get_features(
            &terms,
            "Some title",
            "https://example.com/",
            "Some extract",
            1.0,
            true,
        );
        assert_eq!(features.len(), NUM_FEATURES);
    }
}
