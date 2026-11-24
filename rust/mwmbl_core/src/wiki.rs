//! Wikipedia page view scores, as `get_wiki_score` in mwmbl/tinysearchengine/rank.py gives
//! them.
//!
//! The scores are a sorted table that build.rs generates from `wiki_stats.json`; see there for
//! the layout and why it is not a `HashMap`.

/// The article titles and their scores, split out of the table that build.rs wrote.
struct WikiTable {
    /// Where each title starts in `titles`, and where the last one ends.
    title_offsets: &'static [[u8; 4]],
    scores: &'static [[u8; 8]],
    titles: &'static [u8],
}

/// Split when the crate is compiled, so a table that doesn't match the layout fails the build.
static WIKI_TABLE: WikiTable = {
    let table = include_bytes!(concat!(env!("OUT_DIR"), "/wiki_scores.bin")).as_slice();
    let Some((num_titles, table)) = table.split_first_chunk::<4>() else {
        panic!("The wiki table has no length");
    };
    let num_titles = u32::from_le_bytes(*num_titles) as usize;
    let Some((title_offsets, table)) = table.split_at_checked((num_titles + 1) * 4) else {
        panic!("The wiki table is too short for its title offsets");
    };
    let Some((scores, titles)) = table.split_at_checked(num_titles * 8) else {
        panic!("The wiki table is too short for its scores");
    };
    let (title_offsets, _) = title_offsets.as_chunks::<4>();
    let Some(titles_end) = title_offsets.last() else {
        panic!("The wiki table has no title offsets");
    };
    assert!(
        u32::from_le_bytes(*titles_end) as usize == titles.len(),
        "The wiki table's titles don't end where its offsets say"
    );
    WikiTable {
        title_offsets,
        scores: scores.as_chunks::<8>().0,
        titles,
    }
};

impl WikiTable {
    fn len(&self) -> usize {
        self.scores.len()
    }

    /// The title at `position`, or `None` past the end of the table.
    fn title(&self, position: usize) -> Option<&'static [u8]> {
        let [start, end] = self.title_offsets.get(position..position + 2)? else {
            return None;
        };
        let start = usize::try_from(u32::from_le_bytes(*start)).ok()?;
        let end = usize::try_from(u32::from_le_bytes(*end)).ok()?;
        self.titles.get(start..end)
    }

    fn score(&self, position: usize) -> Option<f64> {
        self.scores.get(position).copied().map(f64::from_le_bytes)
    }

    /// The score of the article called `title`, or `None` for one that isn't in the stats.
    fn lookup(&self, title: &str) -> Option<f64> {
        let target = title.as_bytes();
        let (mut low, mut high) = (0, self.len());
        while low < high {
            let middle = usize::midpoint(low, high);
            match self.title(middle)?.cmp(target) {
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
    fn test_wiki_score_unknown() {
        let score = get_wiki_score("https://en.wikipedia.org/wiki/Totally_Unknown_Article_XYZ");
        assert_eq!(score, 0.0);
    }

    #[test]
    fn test_wiki_score_is_the_normalised_count_of_the_last_path_segment() {
        let python = get_wiki_score("https://en.wikipedia.org/wiki/Python_(programming_language)");
        assert_eq!(python, 0.0009181497562013591);
        assert_eq!(
            get_wiki_score("https://example.com/test"),
            2.490051113958321e-5
        );
        assert_eq!(
            get_wiki_score("Rust_(programming_language)"),
            0.0003626419895055664
        );
        assert_eq!(
            get_wiki_score("https://en.wikipedia.org/wiki/Main_Page"),
            1.0
        );
        assert_eq!(get_wiki_score("https://en.wikipedia.org/wiki/"), 0.0);
        assert_eq!(get_wiki_score(""), 0.0);
    }

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
    fn test_every_title_is_found() {
        assert!(WIKI_TABLE.len() > 100_000, "{}", WIKI_TABLE.len());
        for position in 0..WIKI_TABLE.len() {
            let title = std::str::from_utf8(WIKI_TABLE.title(position).unwrap()).unwrap();
            assert_eq!(WIKI_TABLE.lookup(title), WIKI_TABLE.score(position));
        }
    }
}
