use std::{collections::HashSet, iter::zip};

use itertools::Itertools;
use serde::Serialize;

use crate::indexer::Document;

#[allow(dead_code)]
#[derive(Serialize)]
pub struct Agreement {
    url: String,
    existing_user_id: i32,
    title_similarity: f32,
    extract_similarity: f32,
    extract_empty: bool,
    /// Whether the incoming copy's extract is empty. A copy with an empty extract scores zero
    /// against any other, and a quarter of the agreements replayed from a real crawl had one, so
    /// these need telling apart from copies that disagree
    existing_extract_empty: bool,
    last_crawled_delta: Option<i64>,
    /// Seconds from the stored copy's crawl to the incoming one's: pages change, so a large
    /// gap explains a low similarity that a small one does not. The stored crawl time is the
    /// latest of the copies merged into it, not necessarily that of its text.
    title: String,
    extract: String,
    existing_title: String,
    existing_extract: String,
}

impl Agreement {
    #[must_use]
    pub fn new(
        document: &Document,
        copy_title: &str,
        copy_url: &str,
        copy_extract: &str,
        copy_user_id: i32,
        stored_last_crawled: Option<i64>,
    ) -> Self {
        Self {
            url: copy_url.to_owned(),
            existing_user_id: copy_user_id,
            title_similarity: trigram_similarity(document.title(), copy_title),
            extract_similarity: trigram_similarity(document.extract(), copy_extract),
            extract_empty: document.extract().trim().is_empty(),
            existing_extract_empty: copy_extract.trim().is_empty(),
            last_crawled_delta: document
                .last_crawled()
                .and_then(|v| stored_last_crawled.map(|v2| v - v2)),
            title: document.title().to_owned(),
            extract: document.extract().to_owned(),
            existing_title: copy_title.to_owned(),
            existing_extract: copy_extract.to_owned(),
        }
    }
}

/// Jaccard similarity of the two strings' character trigrams
#[allow(clippy::cast_precision_loss)]
fn trigram_similarity(first: &str, second: &str) -> f32 {
    let first_trigrams = trigrams(first);
    let second_trigrams = trigrams(second);
    first_trigrams
        .intersection(&second_trigrams)
        .collect::<HashSet<_>>()
        .len() as f32
        / first_trigrams
            .union(&second_trigrams)
            .collect::<HashSet<_>>()
            .len() as f32
}

fn trigrams(text: &str) -> HashSet<String> {
    let normalised = text.to_lowercase().split_whitespace().join(" ");
    zip(
        normalised.chars().take(normalised.len() - 2),
        zip(
            normalised.chars().skip(1).take(normalised.len() - 2),
            normalised.chars().skip(2).take(normalised.len() - 2),
        ),
    )
    .map(|(a, (b, c))| format!("{a}{b}{c}"))
    .collect()
}
