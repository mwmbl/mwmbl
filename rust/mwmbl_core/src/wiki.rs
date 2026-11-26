//! Wikipedia page view scores, as `get_wiki_score` in mwmbl/tinysearchengine/rank.py gives
//! them.
//!
//! The scores are a sorted table that build.rs generates from `wiki_stats.json`; see there for
//! the layout and why it is not a `HashMap`.

use std::sync::LazyLock;

include!(concat!(env!("OUT_DIR"), "/wiki_table.rs"));

/// The article titles, sorted bytewise. `SCORES` holds their scores in the same order.
static TITLES: LazyLock<Vec<&'static str>> = LazyLock::new(|| TITLE_LINES.split('\n').collect());

/// The score of the article called `title`, or `None` for one that isn't in the stats.
fn lookup(title: &str) -> Option<f64> {
    let position = TITLES.binary_search(&title).ok()?;
    SCORES.get(position).copied()
}

/// The page view count of the Wikipedia article named by the last path segment of `url`,
/// divided by the largest count: 0 for an article that isn't in the stats.
///
/// The domain is ignored, so any URL ending in an article's title gets its score.
#[must_use]
pub fn get_wiki_score(url: &str) -> f64 {
    let title = url.rsplit_once('/').map_or(url, |(_, title)| title);
    if title.is_empty() {
        return 0.0;
    }
    lookup(title).unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wiki_max_score_normalised() {
        let max = SCORES.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        assert!(
            (max - 1.0).abs() < 1e-9,
            "Max wiki score should be 1.0, got {}",
            max
        );
    }

    #[test]
    fn test_there_is_a_score_for_every_title() {
        assert!(TITLES.len() > 100_000, "{}", TITLES.len());
        assert_eq!(TITLES.len(), SCORES.len());
    }

    #[test]
    fn test_the_titles_are_sorted_and_distinct() {
        assert!(TITLES.is_sorted_by(|a, b| a < b));
    }

    #[test]
    fn test_every_title_gets_its_own_score() {
        for (title, score) in TITLES.iter().zip(&SCORES) {
            assert_eq!(lookup(title), Some(*score), "{title}");
        }
    }
}
