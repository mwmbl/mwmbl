//! The names and order of the features `mwmbl_core` computes for a query and a document.

use mwmbl_core::rank::Features;

/// The feature names in the order of a feature row. Models trained in Rust and with the
/// Python `FeatureExtractor` share this order, so either can score the other's features.
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

pub const NUM_FEATURES: usize = FEATURE_NAMES.len();

/// Index of `num_terms`, the number of query terms, in a feature row.
pub const NUM_TERMS_INDEX: usize = 42;
/// Index of `match_terms`, the most distinct terms matched in any one of the title, extract,
/// domain, tokenized domain and path, in a feature row.
pub const MATCH_TERMS_INDEX: usize = NUM_FEATURES - 1;

/// Staan's own ranking, appended after `FEATURE_NAMES` by a pipeline that uses provider
/// features (Combined Search's). Standard search never sees these columns.
pub const PROVIDER_FEATURE_NAMES: &[&str] = &["in_staan", "staan_rank"];

/// The features as a row, in the order of `FEATURE_NAMES`.
pub(crate) fn feature_row(features: Features) -> [f32; NUM_FEATURES] {
    // SAFETY: `Features` is made of `f32`s only, so every bit pattern is a valid array, and
    // `transmute` fails to compile unless the sizes match. The field order isn't guaranteed
    // without `#[repr(C)]`, which `mwmbl_core` doesn't declare: the golden tests catch a
    // reordering, and `feature_row_follows_the_fields_of_features` ties the order to the names.
    unsafe { std::mem::transmute::<Features, [f32; NUM_FEATURES]>(features) }
}

#[cfg(test)]
mod tests {
    use mwmbl_core::{indexer::Document, rank::get_features};
    use regex::Regex;

    use super::*;

    /// The `{field}_{section}` names and values of `features`, in the order `Debug` prints them.
    fn named_fields(features: &Features) -> Vec<(String, f32)> {
        let debug = format!("{features:?}");
        let section = Regex::new(r"(\w+): MatchFeatures \{([^}]*)\}").unwrap();
        let field = Regex::new(r"(\w+): ([-+\w.]+)").unwrap();
        let mut named = Vec::new();
        for section in section.captures_iter(&debug) {
            for field in field.captures_iter(&section[2]) {
                named.push((
                    format!("{}_{}", &field[1], &section[1]),
                    field[2].parse().unwrap(),
                ));
            }
        }
        let scalars = section.replace_all(&debug, "");
        for field in field.captures_iter(&scalars) {
            named.push((field[1].to_owned(), field[2].parse().unwrap()));
        }
        named
    }

    #[test]
    fn feature_row_follows_the_fields_of_features() {
        let doc = Document::new(
            "Rust programming language".to_owned(),
            "https://www.rust-lang.org/learn/get-started?x=rust".to_owned(),
            "A language empowering everyone to build reliable software".to_owned(),
            Some(2.5),
            None,
            None,
            None,
            None,
            None,
        );
        let features = get_features(&["rust", "language"], &doc, true).unwrap();
        let (names, values): (Vec<String>, Vec<f32>) = named_fields(&features).into_iter().unzip();
        assert_eq!(names, FEATURE_NAMES);
        let value_bits: Vec<u32> = values.iter().map(|value| value.to_bits()).collect();
        assert_eq!(value_bits, feature_row(features).map(f32::to_bits));
    }

    #[test]
    fn indices_name_their_features() {
        assert_eq!(FEATURE_NAMES[NUM_TERMS_INDEX], "num_terms");
        assert_eq!(FEATURE_NAMES[MATCH_TERMS_INDEX], "match_terms");
    }
}
