/// Text processing utilities: tokenization and query regex building.
/// Ports mwmbl/tokenizer.py and the get_query_regex function from mwmbl/format.py.

use regex::RegexBuilder;

/// Clean unicode by round-tripping through UTF-8 (drops invalid bytes).
pub fn clean_unicode(s: &str) -> String {
    // Rust strings are always valid UTF-8, so this is a no-op for well-formed input.
    // We replicate the Python behaviour of silently dropping invalid bytes.
    s.chars().collect()
}

/// Tokenize a query string: lowercase, split on whitespace.
/// Replicates mwmbl/tokenizer.py::tokenize.
pub fn tokenize(input: &str) -> Vec<String> {
    let cleaned = clean_unicode(input);
    let mut tokens: Vec<String> = cleaned.to_lowercase().split_whitespace()
        .map(|s| s.to_string())
        .collect();

    // If the original input ends with '…', discard the last two tokens
    if input.ends_with('…') && tokens.len() >= 2 {
        tokens.truncate(tokens.len() - 2);
    }

    tokens
}

/// Build a regex pattern that matches any of the query terms.
/// Replicates mwmbl/format.py::get_query_regex.
///
/// - `is_url`: use `\b` word boundaries around each term
/// - `is_complete`: last term also gets a trailing boundary; otherwise it's a prefix match
pub fn get_query_regex(terms: &[&str], is_complete: bool, is_url: bool) -> String {
    if terms.is_empty() {
        return String::new();
    }

    let word_sep = if is_url { r"\b" } else { "" };

    let mut patterns: Vec<String> = Vec::with_capacity(terms.len());

    for (i, term) in terms.iter().enumerate() {
        let escaped = regex::escape(term);
        let is_last = i == terms.len() - 1;

        let pat = if is_last && !is_complete {
            // Prefix match for the last term when query is incomplete
            format!("{}{}", word_sep, escaped)
        } else {
            format!("{}{}{}", word_sep, escaped, word_sep)
        };
        patterns.push(pat);
    }

    patterns.join("|")
}

/// Build a compiled case-insensitive regex for the query terms.
/// Returns None if terms is empty.
pub fn build_query_regex(terms: &[&str], is_complete: bool, is_url: bool) -> Option<regex::Regex> {
    let pattern = get_query_regex(terms, is_complete, is_url);
    if pattern.is_empty() {
        return None;
    }
    RegexBuilder::new(&pattern)
        .case_insensitive(true)
        .build()
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tokenize_basic() {
        let tokens = tokenize("Hello World");
        assert_eq!(tokens, vec!["hello", "world"]);
    }

    #[test]
    fn test_tokenize_ellipsis() {
        let tokens = tokenize("hello world foo bar…");
        // Last two tokens dropped
        assert_eq!(tokens, vec!["hello", "world"]);
    }

    #[test]
    fn test_get_query_regex_complete() {
        let terms = vec!["rust", "lang"];
        let pattern = get_query_regex(&terms, true, false);
        assert!(pattern.contains("rust"));
        assert!(pattern.contains("lang"));
        assert!(pattern.contains('|'));
    }

    #[test]
    fn test_get_query_regex_incomplete() {
        let terms = vec!["rust", "lan"];
        let pattern = get_query_regex(&terms, false, false);
        // Last term should be a prefix (no trailing boundary)
        assert!(pattern.ends_with("lan"));
    }

    #[test]
    fn test_get_query_regex_url() {
        let terms = vec!["rust"];
        let pattern = get_query_regex(&terms, true, true);
        assert!(pattern.contains(r"\b"));
    }

    #[test]
    fn test_build_query_regex_matches() {
        let terms = vec!["rust"];
        let re = build_query_regex(&terms, true, false).unwrap();
        assert!(re.is_match("I love Rust programming"));
        assert!(!re.is_match("I love Python programming"));
    }
}
