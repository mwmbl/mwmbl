//! Wikipedia page view scores, as `get_wiki_score` in mwmbl/tinysearchengine/rank.py gives
//! them.
//!
//! The scores are a sorted table that build.rs generates from `wiki_stats.json`; see there for
//! the layout and why it is not a `HashMap`.

include!(concat!(env!("OUT_DIR"), "/wiki_table.rs"));

/// The article titles, sorted, and their scores.
struct WikiTable {
    /// Where each title starts in `titles`, and where the last one ends.
    title_offsets: &'static [u32],
    scores: &'static [f64],
    titles: &'static str,
}

static WIKI_TABLE: WikiTable = WikiTable {
    title_offsets: &TITLE_OFFSETS,
    scores: &SCORES,
    titles: TITLES,
};

impl WikiTable {
    fn len(&self) -> usize {
        self.scores.len()
    }

    /// The title at `position`, or `None` past the end of the table.
    fn title(&self, position: usize) -> Option<&'static str> {
        let [start, end] = self.title_offsets.get(position..position + 2)? else {
            return None;
        };
        self.titles.get(*start as usize..*end as usize)
    }

    fn score(&self, position: usize) -> Option<f64> {
        self.scores.get(position).copied()
    }

    /// The score of the article called `title`, or `None` for one that isn't in the stats.
    fn lookup(&self, title: &str) -> Option<f64> {
        let (mut low, mut high) = (0, self.len());
        while low < high {
            let middle = usize::midpoint(low, high);
            match self.title(middle)?.cmp(title) {
                std::cmp::Ordering::Less => low = middle + 1,
                std::cmp::Ordering::Greater => high = middle,
                std::cmp::Ordering::Equal => return self.score(middle),
            }
        }
        None
    }
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
    WIKI_TABLE.lookup(title).unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wiki_max_score_normalised() {
        let max = (0..WIKI_TABLE.len())
            .map(|position| WIKI_TABLE.score(position).unwrap())
            .fold(f64::NEG_INFINITY, f64::max);
        assert!(
            (max - 1.0).abs() < 1e-9,
            "Max wiki score should be 1.0, got {}",
            max
        );
    }

    #[test]
    fn test_the_titles_end_where_the_offsets_say() {
        assert_eq!(
            WIKI_TABLE.title_offsets.last().map(|end| *end as usize),
            Some(WIKI_TABLE.titles.len())
        );
    }

    #[test]
    fn test_the_title_offsets_never_decrease_so_every_title_is_readable() {
        for (position, pair) in WIKI_TABLE.title_offsets.windows(2).enumerate() {
            assert!(pair[0] <= pair[1], "offset {position}: {pair:?}");
        }
        for position in 0..WIKI_TABLE.len() {
            assert!(WIKI_TABLE.title(position).is_some(), "title {position}");
        }
    }

    #[test]
    fn test_every_title_is_found() {
        assert!(WIKI_TABLE.len() > 100_000, "{}", WIKI_TABLE.len());
        for position in 0..WIKI_TABLE.len() {
            let title = WIKI_TABLE.title(position).unwrap();
            assert_eq!(WIKI_TABLE.lookup(title), WIKI_TABLE.score(position));
        }
    }
}
