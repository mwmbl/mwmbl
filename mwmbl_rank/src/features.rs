use bytemuck::{Pod, Zeroable};
use mwmbl_core::{rank::Features, wiki::get_wiki_score};
use regex::Regex;
/// Feature extraction for learning-to-rank.
/// Ports get_features, get_match_features, and score_match from
/// mwmbl/tinysearchengine/rank.py.
use std::collections::HashMap;
use url::Url;

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

/// Index of `num_terms` (number of query terms) in the feature vector.
pub const NUM_TERMS_INDEX: usize = 42;
/// Index of `match_terms` (max distinct terms matched over title/extract/domain/
/// domain_tokenized/path) in the feature vector.
pub const MATCH_TERMS_INDEX: usize = NUM_FEATURES - 1;

/// Staan's own ranking, appended after FEATURE_NAMES by a pipeline that uses provider
/// features (Combined Search's). Standard search never sees these columns.
pub const PROVIDER_FEATURE_NAMES: &[&str] = &["in_staan", "staan_rank"];

#[derive(Copy, Clone)]
pub(crate) struct MyFeatures(Features);

unsafe impl Pod for MyFeatures {}
unsafe impl Zeroable for MyFeatures {}

pub(crate) fn features_vec(p: Features) -> [f32; 50] {
    bytemuck::cast::<MyFeatures,[f32; 50]>(MyFeatures(p))
}
