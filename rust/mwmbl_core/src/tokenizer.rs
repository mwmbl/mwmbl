use std::{cmp::min, iter::zip};

#[must_use]
pub fn tokenize(input_text: &str) -> Vec<String> {
    let cleaned_text = clean_unicode(input_text);
    let mut tokens: Vec<String> = cleaned_text
        .to_lowercase()
        .split_whitespace()
        .map(std::borrow::ToOwned::to_owned)
        .collect();
    if input_text.ends_with("…") {
        // Discard the last two tokens since there will likely be a word cut in two
        tokens.pop();
        tokens.pop();
    }
    tokens
}

#[must_use]
pub fn get_bigrams(num_bigrams: usize, tokens: &[String]) -> Vec<String> {
    let num_bigrams = min(num_bigrams, tokens.len() - 1);
    zip(
        tokens.iter().take(num_bigrams),
        tokens.iter().skip(1).take(num_bigrams),
    )
    .map(|(a, b)| format!("{a} {b}"))
    .collect()
}

#[must_use]
pub fn clean_unicode(input_text: &str) -> String {
    input_text.escape_unicode().to_string()
}
