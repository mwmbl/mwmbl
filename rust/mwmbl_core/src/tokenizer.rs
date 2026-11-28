/// Sentence punctuation that sticks to a word, as in "(see" or "wikipedia,". Stripping it
/// lets the word match a query. Only these characters are stripped, so "c++", "c#",
/// "--global" and ".net" keep theirs. The same as `mwmbl/tokenizer.py`.
const LEADING_PUNCTUATION: &[char] = &['(', '[', '{', '"', '\'', '“', '‘', '«'];
const TRAILING_PUNCTUATION: &[char] = &[
    '.', ',', ';', ':', '!', '?', ')', ']', '}', '"', '\'', '”', '’', '»',
];

#[must_use]
pub fn tokenize(input_text: &str) -> Vec<String> {
    let cleaned_text = clean_unicode(input_text);
    let mut tokens: Vec<String> = cleaned_text
        .to_lowercase()
        .split(is_python_whitespace)
        .filter(|token| !token.is_empty())
        .map(|token| strip_punctuation(token).to_owned())
        .collect();
    if input_text.ends_with("…") {
        // Discard the last two tokens since there will likely be a word cut in two
        tokens.truncate(tokens.len().saturating_sub(2));
    }
    tokens
}

/// Whitespace as Python's `str.split()` sees it, which also splits on the information
/// separators U+001C to U+001F.
fn is_python_whitespace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `token` without its leading and trailing sentence punctuation, or `token` itself if it is
/// nothing but punctuation, like "-".
#[must_use]
pub fn strip_punctuation(token: &str) -> &str {
    let stripped = token
        .trim_start_matches(LEADING_PUNCTUATION)
        .trim_end_matches(TRAILING_PUNCTUATION);
    if stripped.is_empty() { token } else { stripped }
}

#[must_use]
pub fn get_bigrams(num_bigrams: usize, tokens: &[String]) -> Vec<String> {
    tokens
        .windows(2)
        .take(num_bigrams)
        .map(|pair| pair.join(" "))
        .collect()
}

#[must_use]
pub fn clean_unicode(input_text: &str) -> String {
    input_text.chars().collect()
}
