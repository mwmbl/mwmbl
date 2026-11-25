#[must_use]
pub fn tokenize(input_text: &str) -> Vec<String> {
    let cleaned_text = clean_unicode(input_text);
    let mut tokens: Vec<String> = cleaned_text
        .to_lowercase()
        .split_whitespace()
        .map(std::borrow::ToOwned::to_owned)
        .collect();
    if input_text.ends_with("…") && tokens.len() >= 2 {
        // Discard the last two tokens since there will likely be a word cut in two
        tokens.truncate(tokens.len() - 2);
    }
    tokens
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
