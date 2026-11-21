use std::{
    cmp::max,
    collections::{HashMap, HashSet},
    convert::Infallible,
};

use anyhow::bail;
use regex::{Regex, RegexBuilder};
use reqwest::Url;

use crate::{
    constants::{
        DOMAIN_SCORE_SMOOTHING, MATCH_EXPONENT, MATCH_SCORE_THRESHOLD, SCORE_LENGTH_PENALTY,
    },
    hn_top_domains_filtered::{DOMAIN_MAX_SCORE, DOMAIN_MIN_SCORE, DOMAINS},
    indexer::{Document, DocumentState},
    term_matcher::TermMatcher,
    wiki::get_wiki_score,
};

pub trait Ranker {
    fn order_results(
        &self,
        terms: &[&str],
        results: &[Document],
        is_complete: bool,
    ) -> Vec<Document>;
}

pub struct HeuristicRanker {
    score_threshold: f32,
}

impl HeuristicRanker {
    #[must_use]
    pub fn new(score_threshold: f32) -> Self {
        Self { score_threshold }
    }
}

impl Ranker for HeuristicRanker {
    fn order_results(
        &self,
        terms: &[&str],
        results: &[Document],
        is_complete: bool,
    ) -> Vec<Document> {
        if results.is_empty() {
            return Vec::new();
        }
        let matchers = QueryMatchers::new(terms, is_complete);
        let mut results_and_scores: Vec<(f32, &Document)> = results
            .iter()
            .map(|doc| (score_result(&matchers, doc), doc))
            .collect();
        results_and_scores.sort_by_key(|v| ordered_float::OrderedFloat(v.0));
        results_and_scores
            .into_iter()
            .filter_map(|(score, doc)| {
                if score > self.score_threshold {
                    Some(doc.clone())
                } else {
                    None
                }
            })
            .collect()
    }
}

#[allow(clippy::cast_precision_loss)]
fn score_result(matchers: &QueryMatchers, result: &Document) -> f32 {
    let Ok(features) = get_features_with(matchers, result) else {
        eprintln!("Cannot score result");
        return 0.0;
    };

    let length_penalty = (-SCORE_LENGTH_PENALTY * result.url().len() as f32).exp();
    let match_score = 4.0 * features.title.match_score
        + features.extract.match_score
        + 2.0 * features.domain.match_score
        + 2.0 * features.domain_tokenized.match_score
        + features.path.match_score;

    // Documents a person or an external source deliberately attached to a query are exempt
    // from the majority-terms rule: they are here because someone said they belong here, not
    // because they matched. That exemption was written as "state is not None" when every
    // state meant exactly that (FROM_USER, FROM_GOOGLE, FROM_WIKI). The negative states are
    // later bookkeeping - SYNCED_WITH_MAIN_INDEX marks a crawler's local mirror of the main
    // index - and nobody curated those, so they get the rule like anything else.
    let is_curated = result.state().is_some_and(|s| {
        !matches!(
            s,
            DocumentState::DELETED | DocumentState::SyncedWithMainIndex
        )
    });
    if features.match_terms <= (features.num_terms / 2.0) && !is_curated {
        return 0.0;
    }

    if match_score > MATCH_SCORE_THRESHOLD {
        return match_score * length_penalty * (features.domain_score + DOMAIN_SCORE_SMOOTHING)
            / 10.0;
    }

    0.0
}

#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct MatchFeatures {
    last_match_char: f32,
    match_length: f32,
    total_possible_match_length: f32,
    match_score: f32,
    match_terms: f32,
    match_term_proportion: f32,
}

#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct Features {
    title: MatchFeatures,
    extract: MatchFeatures,
    domain: MatchFeatures,
    domain_tokenized: MatchFeatures,
    path: MatchFeatures,
    query: MatchFeatures,
    whole: MatchFeatures,
    num_terms: f32,
    num_chars: f32,
    domain_score: f32,
    path_length: f32,
    domain_length: f32,
    wiki_score: f32,
    item_score: f32,
    match_terms: f32,
}

/// The term matchers for one query: built once, then used to score every document.
pub struct QueryMatchers {
    text: TermMatcher,
    url: TermMatcher,
}

impl QueryMatchers {
    #[must_use]
    pub fn new(terms: &[&str], is_complete: bool) -> Self {
        Self {
            text: TermMatcher::new(terms, is_complete, false),
            url: TermMatcher::new(terms, is_complete, true),
        }
    }
}

pub fn get_features(terms: &[&str], doc: &Document, is_complete: bool) -> anyhow::Result<Features> {
    get_features_with(&QueryMatchers::new(terms, is_complete), doc)
}

/// The same features as [`get_features_regex`], without compiling a regex per document.
pub fn get_features_with(matchers: &QueryMatchers, doc: &Document) -> anyhow::Result<Features> {
    let terms = matchers.text.terms();
    let (url_path, domain, url_query) = parse_url(terms, doc)?;
    let features = assemble_features(
        terms,
        doc,
        &url_path,
        &domain,
        &url_query,
        |text| get_match_features_with(&matchers.text, text),
        |text| get_match_features_with(&matchers.url, text),
    )?;
    Ok(features)
}

/// The path, domain and query of a document's URL, or an error if it can't be scored.
fn parse_url(
    terms: &[impl AsRef<str>],
    doc: &Document,
) -> anyhow::Result<(String, String, String)> {
    if terms.is_empty() {
        bail!("terms must containt at least 1 element");
    }
    let parsed_url = Url::parse(doc.url())?;
    let Some(domain) = parsed_url.domain() else {
        bail!("Url must have a domain");
    };
    Ok((
        parsed_url.path().to_owned(),
        domain.to_owned(),
        parsed_url.query().unwrap_or("").to_owned(),
    ))
}

#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
pub fn get_features_regex(
    terms: &[&str],
    doc: &Document,
    re_text: &Regex,
    re_url: &Regex,
) -> anyhow::Result<Features> {
    let (url_path, domain, url_query) = parse_url(terms, doc)?;
    Ok(get_features_regex_unchecked(
        terms, doc, &url_path, &domain, &url_query, re_text, re_url,
    ))
}

#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
#[must_use]
pub fn get_features_regex_unchecked(
    terms: &[&str],
    doc: &Document,
    url_path: &str,
    url_domain: &str,
    url_query: &str,
    re_text: &Regex,
    re_url: &Regex,
) -> Features {
    let Ok::<_, Infallible>(features) = assemble_features(
        terms,
        doc,
        url_path,
        url_domain,
        url_query,
        |text| Ok(get_match_features_regex(terms, text, re_text)),
        |text| Ok(get_match_features_regex(terms, text, re_url)),
    );
    features
}

/// Builds the features from match features for the text (no word boundaries) and URL parts.
#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
fn assemble_features<E>(
    terms: &[impl AsRef<str>],
    doc: &Document,
    url_path: &str,
    url_domain: &str,
    url_query: &str,
    text_features: impl Fn(&str) -> Result<MatchFeatures, E>,
    url_features: impl Fn(&str) -> Result<MatchFeatures, E>,
) -> Result<Features, E> {
    let whole = doc.title().to_owned()
        + " "
        + doc.extract()
        + " "
        + url_domain
        + " "
        + url_path
        + " "
        + url_query;

    let title = text_features(doc.title())?;
    let extract = text_features(doc.extract())?;
    let domain = url_features(url_domain)?;
    let domain_tokenized = text_features(url_domain)?;
    let path = url_features(url_path)?;
    let query = text_features(url_query)?;
    let whole = text_features(&whole)?;

    let match_terms = max(
        ordered_float::OrderedFloat(title.match_terms),
        ordered_float::OrderedFloat(extract.match_terms),
    );
    let match_terms = max(match_terms, ordered_float::OrderedFloat(domain.match_terms));
    let match_terms = max(
        match_terms,
        ordered_float::OrderedFloat(domain_tokenized.match_terms),
    );
    let match_terms = max(match_terms, ordered_float::OrderedFloat(path.match_terms));
    let match_terms = max(match_terms, ordered_float::OrderedFloat(query.match_terms));
    let match_terms = max(match_terms, ordered_float::OrderedFloat(whole.match_terms));

    Ok(Features {
        title,
        extract,
        domain,
        domain_tokenized,
        path,
        query,
        whole,
        num_terms: terms.len() as f32,
        num_chars: terms.iter().map(|v| v.as_ref().len() as f32).sum::<f32>() + terms.len() as f32
            - 1.0,
        domain_score: get_domain_score(url_domain),
        path_length: url_path.len() as f32,
        domain_length: url_domain.len() as f32,
        wiki_score: get_wiki_score(doc.url()) as f32,
        item_score: doc.score().unwrap_or(0.0),
        match_terms: *match_terms,
    })
}

#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
pub fn get_domain_score(domain: &str) -> f32 {
    let Some(score) = DOMAINS.get(domain) else {
        return 0.0;
    };
    (*score as f32 - *DOMAIN_MIN_SCORE as f32)
        / (*DOMAIN_MAX_SCORE as f32 - *DOMAIN_MIN_SCORE as f32)
}

#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
pub fn get_match_features(
    terms: &[&str],
    result_string: &str,
    is_complete: bool,
    is_url: bool,
) -> anyhow::Result<MatchFeatures> {
    let Ok(query_regex) = get_query_regex(terms, is_complete, is_url) else {
        bail!("cannot build regex");
    };
    Ok(get_match_features_regex(terms, result_string, &query_regex))
}

#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
#[must_use]
pub fn get_match_features_regex(
    terms: &[&str],
    result_string: &str,
    query_regex: &Regex,
) -> MatchFeatures {
    let matches = query_regex.find_iter(result_string);

    let mut last_match_char: f32 = 1.0;
    let mut seen_matches = HashSet::new();
    let mut match_length: f32 = 0.0;
    let mut match_counts: HashMap<String, i32> = HashMap::new();
    for match_value in matches {
        let value = match_value.clone().as_str().to_lowercase();
        match match_counts.entry(value.clone()) {
            std::collections::hash_map::Entry::Occupied(mut occupied_entry) => {
                occupied_entry.insert(occupied_entry.get() + 1)
            }
            std::collections::hash_map::Entry::Vacant(vacant_entry) => *vacant_entry.insert(1),
        };
        if !seen_matches.contains(&value) {
            last_match_char = match_value.end() as f32;
            seen_matches.insert(value.clone());
            match_length += value.len() as f32;
        }
    }

    let term_lengths = terms.iter().map(|term| term.len());
    match_features(
        term_lengths,
        last_match_char,
        match_length,
        seen_matches.len(),
    )
}

/// The same match features as [`get_match_features_regex`], without compiling a regex.
#[allow(clippy::cast_precision_loss)]
pub fn get_match_features_with(
    matcher: &TermMatcher,
    result_string: &str,
) -> Result<MatchFeatures, regex::Error> {
    let mut last_match_char: f32 = 1.0;
    let mut match_length: f32 = 0.0;
    let mut seen_term_ids: Vec<usize> = Vec::new();
    matcher.for_each_match(result_string, |found| {
        if !seen_term_ids.contains(&found.term_id) {
            seen_term_ids.push(found.term_id);
            last_match_char = found.end as f32;
            match_length += found.len as f32;
        }
    })?;
    let term_lengths = matcher.terms().iter().map(String::len);
    Ok(match_features(
        term_lengths,
        last_match_char,
        match_length,
        seen_term_ids.len(),
    ))
}

#[allow(clippy::cast_precision_loss)]
fn match_features(
    term_lengths: impl ExactSizeIterator<Item = usize>,
    last_match_char: f32,
    match_length: f32,
    num_distinct_matches: usize,
) -> MatchFeatures {
    let num_terms = term_lengths.len();
    let total_possible_match_length = term_lengths.map(|len| len as f32).sum();
    let match_score =
        MATCH_EXPONENT.powf(match_length - total_possible_match_length) / last_match_char;
    let match_terms = num_distinct_matches as f32;
    let match_term_proportion = match_terms / num_terms as f32;
    MatchFeatures {
        last_match_char,
        match_length,
        total_possible_match_length,
        match_score,
        match_terms,
        match_term_proportion,
    }
}

pub fn get_query_regex(
    terms: &[&str],
    is_complete: bool,
    use_word_boundaries: bool,
) -> Result<Regex, regex::Error> {
    if terms.is_empty() {
        return Regex::new("");
    }

    let word_sep = if use_word_boundaries { r"\b" } else { "" };
    let trailing_sep = if is_complete { word_sep } else { "" };
    let term_patterns: Vec<String> = query_pattern_terms(terms, is_complete)
        .iter()
        .map(|term| format!("{word_sep}{}{trailing_sep}", regex::escape(term)))
        .collect();
    RegexBuilder::new(&term_patterns.join("|"))
        .case_insensitive(true)
        .build()
}

/// The terms that [`get_query_regex`] tries, in order. Each has `\b` before it when using word
/// boundaries, and after it as well when the query is complete.
///
/// For an incomplete query this is every term after the first, then the last term again, so the
/// first term is never matched. That differs from the Python version, which keeps every term and
/// only leaves out the `\b` after the last one.
pub(crate) fn query_pattern_terms<'a>(terms: &[&'a str], is_complete: bool) -> Vec<&'a str> {
    if is_complete {
        return terms.to_vec();
    }
    terms.iter().skip(1).chain(terms.last()).copied().collect()
}
