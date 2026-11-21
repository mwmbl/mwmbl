//! Finds query terms in text without compiling a regex.
//!
//! The ranker used to build the case-insensitive regex `term1|term2|...` (with `\b` around each
//! term for URLs) for every document it scored, and compiling those regexes was almost all of
//! the indexer's time. A [`TermMatcher`] is built once per term list and finds the same matches
//! as that regex by comparing characters directly. It uses the regex crate's own tables, from
//! `regex-syntax`, for case folding and for what `\b` counts as a word character, so it agrees
//! with the regex on non-ASCII text too.

use std::sync::{LazyLock, OnceLock};

use regex::Regex;
use regex_syntax::hir::{ClassUnicode, ClassUnicodeRange};

use crate::rank::{get_query_regex, query_pattern_terms};

/// One match of a term.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TermMatch {
    /// Within one call of [`TermMatcher::for_each_match`], matches with the same `term_id` are
    /// the same text once lowercased.
    pub term_id: usize,
    /// The byte offset where the match ends.
    pub end: usize,
    /// The length in bytes of the lowercased match.
    pub len: usize,
}

pub struct TermMatcher {
    terms: Vec<String>,
    /// For each term the regex tries, in order (see [`query_pattern_terms`]), the characters
    /// that match each of its characters.
    patterns: Vec<Vec<CaseFolded>>,
    /// For each byte, whether a match can start with it, to skip most positions quickly.
    start_bytes: [bool; 256],
    is_complete: bool,
    use_word_boundaries: bool,
    /// Only for no terms or an empty term, which match the empty string everywhere.
    regex: Option<OnceLock<Result<Regex, regex::Error>>>,
}

impl TermMatcher {
    #[must_use]
    pub fn new(terms: &[&str], is_complete: bool, use_word_boundaries: bool) -> Self {
        let patterns = query_pattern_terms(terms, is_complete);
        let needs_regex = patterns.is_empty() || patterns.iter().any(|pattern| pattern.is_empty());
        let patterns: Vec<Vec<CaseFolded>> = patterns
            .iter()
            .map(|pattern| pattern.chars().map(CaseFolded::new).collect())
            .collect();
        let start_bytes = start_bytes(&patterns);
        Self {
            terms: terms.iter().map(|&term| term.to_owned()).collect(),
            patterns,
            start_bytes,
            is_complete,
            use_word_boundaries,
            regex: needs_regex.then(OnceLock::new),
        }
    }

    #[must_use]
    pub fn terms(&self) -> &[String] {
        &self.terms
    }

    /// Calls `on_match` for each match, in order, exactly as `find_iter` on the regex would.
    pub fn for_each_match(
        &self,
        text: &str,
        mut on_match: impl FnMut(TermMatch),
    ) -> Result<(), regex::Error> {
        let mut seen = DistinctMatches::default();
        if let Some(regex) = &self.regex {
            let terms: Vec<&str> = self.terms.iter().map(String::as_str).collect();
            let regex = regex
                .get_or_init(|| get_query_regex(&terms, self.is_complete, self.use_word_boundaries))
                .as_ref()
                .map_err(Clone::clone)?;
            for found in regex.find_iter(text) {
                on_match(seen.add(found.as_str(), found.end()));
            }
            return Ok(());
        }
        // Leftmost-first, like regex alternation: at the first position where any term matches,
        // take the first term in the list that does, then carry on after it.
        let mut start = 0;
        while let Some(&byte) = text.as_bytes().get(start) {
            if self.start_bytes.get(usize::from(byte)) == Some(&false) {
                start += 1;
                continue;
            }
            let Some(next_char) = text.get(start..).and_then(|rest| rest.chars().next()) else {
                break;
            };
            match self
                .patterns
                .iter()
                .find_map(|p| self.match_len(text, start, p))
            {
                Some(len) => {
                    let end = start + len;
                    on_match(seen.add(text.get(start..end).unwrap_or_default(), end));
                    start = end;
                }
                None => start += next_char.len_utf8(),
            }
        }
        Ok(())
    }

    /// The length in bytes of `pattern` matched at `start`, if it matches there.
    fn match_len(&self, text: &str, start: usize, pattern: &[CaseFolded]) -> Option<usize> {
        let rest = text.get(start..)?;
        let mut chars = rest.char_indices();
        for folded in pattern {
            let (_, c) = chars.next()?;
            if !folded.matches(c) {
                return None;
            }
        }
        let len = chars.next().map_or(rest.len(), |(i, _)| i);
        // get_query_regex puts `\b` after a term only for a complete query.
        let boundaries_hold = !self.use_word_boundaries
            || (is_word_boundary(text, start)
                && (!self.is_complete || is_word_boundary(text, start + len)));
        boundaries_hold.then_some(len)
    }
}

/// The distinct lowercased matches so far, numbered in the order they were first seen.
#[derive(Default)]
struct DistinctMatches(Vec<String>);

impl DistinctMatches {
    fn add(&mut self, matched: &str, end: usize) -> TermMatch {
        let position = if matched.is_ascii() {
            // Everything stored is lowercase, so this compares the lowercased match.
            self.0
                .iter()
                .position(|seen| seen.eq_ignore_ascii_case(matched))
        } else {
            let lowercase = matched.to_lowercase();
            self.0.iter().position(|seen| *seen == lowercase)
        };
        let term_id = position.unwrap_or_else(|| {
            self.0.push(matched.to_lowercase());
            self.0.len() - 1
        });
        TermMatch {
            term_id,
            end,
            len: self.0.get(term_id).map_or(0, String::len),
        }
    }
}

/// The characters equal to one character of a term ignoring case, as the regex compiles a
/// case-insensitive literal.
enum CaseFolded {
    Ascii(u8),
    Other(ClassUnicode),
}

/// The case folding of each ASCII character, worked out once rather than for every term.
static ASCII_CASE_FOLDED: LazyLock<Vec<ClassUnicode>> = LazyLock::new(|| {
    (0..=127)
        .map(|byte| case_folded(char::from(byte)))
        .collect()
});

impl CaseFolded {
    fn new(c: char) -> Self {
        match u8::try_from(c) {
            Ok(byte) if byte.is_ascii() => Self::Ascii(byte),
            _ => Self::Other(case_folded(c)),
        }
    }

    fn matches(&self, c: char) -> bool {
        match self {
            Self::Ascii(byte) if c.is_ascii() => {
                u8::try_from(c).is_ok_and(|c| c.eq_ignore_ascii_case(byte))
            }
            // A few non-ASCII characters fold to ASCII ones, such as the Kelvin sign to k.
            _ => self.class().is_some_and(|class| contains(class, c)),
        }
    }

    fn class(&self) -> Option<&ClassUnicode> {
        match self {
            Self::Ascii(byte) => ASCII_CASE_FOLDED.get(usize::from(*byte)),
            Self::Other(class) => Some(class),
        }
    }
}

/// The first UTF-8 byte of every character that the first character of some pattern matches.
/// Continuation bytes are never among them, so a marked byte always starts a character.
fn start_bytes(patterns: &[Vec<CaseFolded>]) -> [bool; 256] {
    let mut start_bytes = [false; 256];
    let first_classes = patterns.iter().filter_map(|p| p.first()?.class());
    for range in first_classes.flat_map(ClassUnicode::ranges) {
        for c in range.start()..=range.end() {
            let first_byte = c.encode_utf8(&mut [0; 4]).as_bytes().first().copied();
            if let Some(starts) = first_byte.and_then(|b| start_bytes.get_mut(usize::from(b))) {
                *starts = true;
            }
        }
    }
    start_bytes
}

fn case_folded(c: char) -> ClassUnicode {
    let mut class = ClassUnicode::new([ClassUnicodeRange::new(c, c)]);
    class.case_fold_simple();
    class
}

fn contains(class: &ClassUnicode, c: char) -> bool {
    class
        .ranges()
        .iter()
        .any(|range| range.start() <= c && c <= range.end())
}

/// `\b` at byte `position`: a word character on exactly one side of it.
fn is_word_boundary(text: &str, position: usize) -> bool {
    let before = text.get(..position).and_then(|s| s.chars().next_back());
    let after = text.get(position..).and_then(|s| s.chars().next());
    is_word_char(before) != is_word_char(after)
}

fn is_word_char(c: Option<char>) -> bool {
    c.is_some_and(|c| {
        if c.is_ascii() {
            c.is_ascii_alphanumeric() || c == '_'
        } else {
            regex_syntax::is_word_character(c)
        }
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::TermMatcher;
    use crate::rank::{get_match_features_regex, get_match_features_with, get_query_regex};

    /// Text built from characters that exercise case folding and word boundaries.
    fn random_text(state: &mut u64, max_len: u64) -> String {
        // Kelvin sign and long s fold to ASCII; sigma has three cases; dotted capital I lowercases
        // to two characters.
        const CHARS: [char; 20] = [
            'k', 's', 'i', 'K', 'S', '_', '-', ' ', '.', '/', '1', 'é', 'É', 'ſ', '\u{212A}', 'σ',
            'ς', 'Σ', 'İ', 'ı',
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
}
