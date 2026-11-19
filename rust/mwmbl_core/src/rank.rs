use std::{
    cmp::max,
    collections::{HashMap, HashSet},
};

use anyhow::bail;
use regex::{Regex, RegexBuilder};
use reqwest::Url;

use crate::{
    constants::MATCH_EXPONENT,
    hn_top_domains_filtered::{DOMAIN_MAX_SCORE, DOMAIN_MIN_SCORE, DOMAINS},
    indexer::Document,
    wiki::get_wiki_score,
};

pub trait Ranker {
    fn order_results(
        &self,
        terms: Vec<String>,
        pages: Vec<Document>,
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
        terms: Vec<String>,
        results: Vec<Document>,
        is_complete: bool,
    ) -> Vec<Document> {
        if results.is_empty() {
            return Vec::new();
        }
        todo!();
        // let results_and_scores = results.into_iter().map(||);

        Vec::new()
    }
}

fn score_result(terms: &[String], result: &Document, is_complete: bool) {
    todo!();
    // let features = get_features(terms, &result, is_complete);

    // length_penalty = math.e ** (-LENGTH_PENALTY * len(result.url))
    // match_score = (
    //     4 * features["match_score_title"]
    //     + features["match_score_extract"]
    //     + 2 * features["match_score_domain"]
    //     + 2 * features["match_score_domain_tokenized"]
    //     + features["match_score_path"]
    // )

    // Documents a person or an external source deliberately attached to a query are exempt
    // from the majority-terms rule: they are here because someone said they belong here, not
    // because they matched. That exemption was written as "state is not None" when every
    // state meant exactly that (FROM_USER, FROM_GOOGLE, FROM_WIKI). The negative states are
    // later bookkeeping - SYNCED_WITH_MAIN_INDEX marks a crawler's local mirror of the main
    // index - and nobody curated those, so they get the rule like anything else.
    // is_curated = result.state is not None and result.state > 0
    // if features["match_terms"] <= len(terms) / 2 and not is_curated:
    //     return 0.0

    // if match_score > MATCH_SCORE_THRESHOLD:
    //     return match_score * length_penalty * (features["domain_score"] + DOMAIN_SCORE_SMOOTHING) / 10

    // return 0.0
}

struct MatchFeatures {
    last_match_char: f32,
    match_length: f32,
    total_possible_match_length: f32,
    match_score: f32,
    match_terms: f32,
    match_term_proportion: f32,
}

struct Features {
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

#[allow(clippy::cast_precision_loss,clippy::cast_possible_truncation)]
fn get_features(
    terms: &[String],
    doc: &Document,
    is_complete: bool,
) -> anyhow::Result<Features> {
    if terms.is_empty() {
        bail!("terms must containt at least 1 element");
    }
    let parsed_url = Url::parse(doc.url())?;
    let query = parsed_url.query().unwrap_or("");
    let Some(domain_str) = parsed_url.domain() else {
        bail!("Url must have a domain");
    };

    let whole = doc.title().to_owned()
        + " "
        + doc.extract()
        + " "
        + domain_str
        + " "
        + parsed_url.path()
        + " "
        + query;

    let title = get_match_features(terms, doc.title(), is_complete, false)?;
    let extract = get_match_features(terms, doc.extract(), is_complete, false)?;
    let domain = get_match_features(terms, domain_str, is_complete, true)?;
    let domain_tokenized = get_match_features(terms, domain_str, is_complete, false)?;
    let path = get_match_features(terms, parsed_url.path(), is_complete, true)?;
    let query = get_match_features(terms, query, is_complete, false)?;
    let whole = get_match_features(terms, &whole, is_complete, false)?;

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
        num_chars: terms.iter().map(|v| v.len() as f32).sum::<f32>() + terms.len() as f32 - 1.0,
        domain_score: get_domain_score(domain_str),
        path_length: parsed_url.path().len() as f32,
        domain_length: domain_str.len() as f32,
        wiki_score: get_wiki_score(doc.url()) as f32,
        item_score: doc.score().unwrap_or(0.0),
        match_terms: *match_terms,
    })
}

#[allow(clippy::cast_precision_loss,clippy::cast_possible_truncation)]
fn get_domain_score(domain: &str) -> f32 {
    let Some(score) = DOMAINS.get(domain) else {
        return 0.0;
    };
    (*score as f32 - *DOMAIN_MIN_SCORE as f32)
        / (*DOMAIN_MAX_SCORE as f32 - *DOMAIN_MIN_SCORE as f32)
}

#[allow(clippy::cast_precision_loss,clippy::cast_possible_truncation)]
fn get_match_features(
    terms: &[String],
    result_string: &str,
    is_complete: bool,
    is_url: bool,
) -> anyhow::Result<MatchFeatures> {
    let Ok(query_regex) = get_query_regex(terms, is_complete, is_url) else {
        bail!("cannot build regex");
    };
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

    let total_possible_match_length = terms.iter().map(|v| v.len() as f32).sum();
    let match_score = MATCH_EXPONENT.powf(match_length - total_possible_match_length)
        / last_match_char;
    let match_terms = seen_matches.len() as f32;
    let match_term_proportion = match_terms / terms.len() as f32;
    Ok(MatchFeatures {
        last_match_char,
        match_length,
        total_possible_match_length,
        match_score,
        match_terms,
        match_term_proportion,
    })
}

fn get_query_regex(
    terms: &[String],
    is_complete: bool,
    use_word_boundaries: bool,
) -> Result<Regex, regex::Error> {
    if terms.is_empty() {
        return Regex::new("");
    }

    let word_sep = if use_word_boundaries { r"\b" } else { "" };

    let term_patterns: Vec<String> = if is_complete {
        terms
            .iter()
            .map(|term| format!("{word_sep}{}{word_sep}", regex::escape(term)))
            .collect()
    } else {
        let mut v: Vec<String> = terms
            .iter()
            .rev()
            .take(terms.len() - 1)
            .map(|term| format!("{word_sep}{}{word_sep}", regex::escape(term)))
            .rev()
            .collect();
        v.push(format!(
            "{word_sep}{}",
            regex::escape(terms.last().map_or("", |v| v))
        ));
        v
    };
    RegexBuilder::new(&term_patterns.join("|"))
        .case_insensitive(false)
        .build()
}
