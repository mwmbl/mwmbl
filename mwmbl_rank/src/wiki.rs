/// Wikipedia score lookup.
/// Replicates get_wiki_score from mwmbl/tinysearchengine/rank.py.
///
/// The scores are a sorted table that build.rs generates from wiki_stats.json; see there for
/// the layout and why it is not a HashMap.

static WIKI_TABLE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/wiki_scores.bin"));

fn read_u32(at: usize) -> usize {
    u32::from_le_bytes(WIKI_TABLE[at..at + 4].try_into().unwrap()) as usize
}

fn len() -> usize {
    read_u32(0)
}

fn key(i: usize) -> &'static [u8] {
    let n = len();
    let keys_start = 4 + (n + 1) * 4 + n * 8;
    &WIKI_TABLE[keys_start + read_u32(4 + i * 4)..keys_start + read_u32(4 + (i + 1) * 4)]
}

fn score(i: usize) -> f64 {
    let at = 4 + (len() + 1) * 4 + i * 8;
    f64::from_le_bytes(WIKI_TABLE[at..at + 8].try_into().unwrap())
}

/// The page view count of an article divided by the largest, or 0 for one not in the stats.
fn lookup(title: &str) -> f64 {
    let target = title.as_bytes();
    let (mut low, mut high) = (0, len());
    while low < high {
        let mid = low + (high - low) / 2;
        match key(mid).cmp(target) {
            std::cmp::Ordering::Less => low = mid + 1,
            std::cmp::Ordering::Greater => high = mid,
            std::cmp::Ordering::Equal => return score(mid),
        }
    }
    0.0
}

/// Get a normalised wiki score for the given URL.
/// Extracts the last path segment as the Wikipedia article title.
pub fn get_wiki_score(url: &str) -> f64 {
    let title = url.split('/').next_back().unwrap_or("");
    if title.is_empty() {
        return 0.0;
    }
    lookup(title)
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
    fn test_wiki_score_range() {
        // Any known article should be in [0, 1]
        let score = get_wiki_score("https://en.wikipedia.org/wiki/Python_(programming_language)");
        assert!(score > 0.0);
        assert!(score <= 1.0);
    }

    #[test]
    fn test_wiki_max_score_normalised() {
        let max = (0..len()).map(score).fold(f64::NEG_INFINITY, f64::max);
        assert!((max - 1.0).abs() < 1e-9, "Max wiki score should be 1.0, got {}", max);
    }

    #[test]
    fn test_every_key_is_found() {
        for i in 0..len() {
            let title = std::str::from_utf8(key(i)).unwrap();
            assert_eq!(lookup(title), score(i));
        }
    }
}
