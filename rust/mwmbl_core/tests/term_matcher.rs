use mwmbl_core::rank::{get_match_features_regex, get_match_features_with, get_query_regex};
use mwmbl_core::term_matcher::TermMatcher;

/// Text built from characters that exercise case folding and word boundaries.
fn random_text(state: &mut u64, max_len: u64) -> String {
    // Kelvin sign and long s fold to ASCII; sigma has three cases; dotted capital I lowercases
    // to two characters.
    const CHARS: [char; 20] = [
        'k', 's', 'i', 'K', 'S', '_', '-', ' ', '.', '/', '1', 'é', 'É', 'ſ', '\u{212A}', 'σ', 'ς',
        'Σ', 'İ', 'ı',
    ];
    let mut next = || {
        *state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1);
        *state >> 33
    };
    let len = next() % max_len;
    // Half the texts are ASCII, the common case.
    let num_chars = if next() % 2 == 0 { 11 } else { 20 };
    (0..len)
        .filter_map(|_| usize::try_from(next() % num_chars).ok())
        .filter_map(|i| CHARS.get(i).copied())
        .collect()
}

#[test]
fn matches_the_same_as_the_regex() {
    let mut state = 7;
    for _ in 0..20_000 {
        // Sometimes no terms, or an empty term, which the matcher leaves to the regex.
        let terms: Vec<String> = (0..state % 4).map(|_| random_text(&mut state, 4)).collect();
        let terms: Vec<&str> = terms.iter().map(String::as_str).collect();
        let text = random_text(&mut state, 40);
        for is_complete in [true, false] {
            for use_word_boundaries in [true, false] {
                let regex = get_query_regex(&terms, is_complete, use_word_boundaries).unwrap();
                let matcher = TermMatcher::new(&terms, is_complete, use_word_boundaries);
                assert_eq!(
                    get_match_features_with(&matcher, &text).unwrap(),
                    get_match_features_regex(&terms, &text, &regex),
                    "terms {terms:?} in {text:?}, complete {is_complete}, \
                     boundaries {use_word_boundaries}"
                );
            }
        }
    }
}

#[test]
fn takes_the_first_listed_term_at_the_leftmost_position() {
    let matcher = TermMatcher::new(&["ki", "kis", "s"], true, false);
    let mut ends = Vec::new();
    matcher
        .for_each_match("xKISkis", |found| ends.push((found.term_id, found.end)))
        .unwrap();
    assert_eq!(ends, [(0, 3), (1, 4), (0, 6), (1, 7)]);
}
