//! Wikipedia page view scores, as `get_wiki_score` in mwmbl/tinysearchengine/rank.py gives
//! them.
//!
//! The scores are a sorted table that build.rs generates from `wiki_stats.json`; see there for
//! the layout and why it is not a `HashMap`.

include!(concat!(env!("OUT_DIR"), "/wiki_table.rs"));

/// The article titles, sorted bytewise.
///
/// They are packed into one string with the offsets alongside rather than stored as a
/// `[&str; N]`, which costs a relocation per title; see build.rs.
struct Titles {
    /// Where each title starts in `text`, and where the last one ends.
    offsets: &'static [u32],
    text: &'static str,
}

impl Titles {
    fn len(&self) -> usize {
        self.offsets.len().saturating_sub(1)
    }

    /// The title at `position`, or `None` past the end.
    fn get(&self, position: usize) -> Option<&'static str> {
        let [start, end] = self.offsets.get(position..position + 2)? else {
            return None;
        };
        self.text.get(*start as usize..*end as usize)
    }

    /// Where `title` is, or `None` if it isn't one of them.
    fn position(&self, title: &str) -> Option<usize> {
        let (mut low, mut high) = (0, self.len());
        while low < high {
            let middle = usize::midpoint(low, high);
            match self.get(middle)?.cmp(title) {
                std::cmp::Ordering::Less => low = middle + 1,
                std::cmp::Ordering::Greater => high = middle,
                std::cmp::Ordering::Equal => return Some(middle),
            }
        }
        None
    }
}

/// The article titles and, at the same positions, their scores.
struct WikiTable {
    titles: Titles,
    scores: &'static [f64],
}

static WIKI_TABLE: WikiTable = WikiTable {
    titles: Titles {
        offsets: &TITLE_OFFSETS,
        text: TITLES,
    },
    scores: &SCORES,
};

impl WikiTable {
    /// The score of the article called `title`, or `None` for one that isn't in the stats.
    fn lookup(&self, title: &str) -> Option<f64> {
        let position = self.titles.position(title)?;
        self.scores.get(position).copied()
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
        let max = WIKI_TABLE
            .scores
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max);
        assert!(
            (max - 1.0).abs() < 1e-9,
            "Max wiki score should be 1.0, got {}",
            max
        );
    }

    #[test]
    fn test_there_is_a_score_for_every_title() {
        assert_eq!(WIKI_TABLE.titles.len(), WIKI_TABLE.scores.len());
    }

    #[test]
    fn test_the_titles_end_where_the_offsets_say() {
        let titles = &WIKI_TABLE.titles;
        assert_eq!(
            titles.offsets.last().map(|end| *end as usize),
            Some(titles.text.len())
        );
    }

    #[test]
    fn test_the_title_offsets_never_decrease_so_every_title_is_readable() {
        let titles = &WIKI_TABLE.titles;
        for (position, pair) in titles.offsets.windows(2).enumerate() {
            assert!(pair[0] <= pair[1], "offset {position}: {pair:?}");
        }
        for position in 0..titles.len() {
            assert!(titles.get(position).is_some(), "title {position}");
        }
    }

    #[test]
    fn test_every_title_is_found_where_it_is() {
        assert!(
            WIKI_TABLE.titles.len() > 100_000,
            "{}",
            WIKI_TABLE.titles.len()
        );
        for position in 0..WIKI_TABLE.titles.len() {
            let title = WIKI_TABLE.titles.get(position).unwrap();
            assert_eq!(WIKI_TABLE.titles.position(title), Some(position));
            assert_eq!(
                WIKI_TABLE.lookup(title),
                WIKI_TABLE.scores.get(position).copied()
            );
        }
    }

    #[test]
    fn test_past_the_end_there_is_no_title() {
        assert_eq!(WIKI_TABLE.titles.get(WIKI_TABLE.titles.len()), None);
    }
}
