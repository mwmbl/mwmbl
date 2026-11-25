use mwmbl_core::tokenizer::get_bigrams;

fn tokens(words: &[&str]) -> Vec<String> {
    words.iter().map(|&word| word.to_owned()).collect()
}

#[test]
fn fewer_than_two_tokens_have_no_bigrams() {
    for words in [&[][..], &["one"][..]] {
        assert!(get_bigrams(10, &tokens(words)).is_empty(), "{words:?}");
    }
}

#[test]
fn bigrams_pair_neighbouring_tokens_up_to_the_limit() {
    let words = tokens(&["a", "b", "c", "d"]);
    let cases = [
        (0, vec![]),
        (2, vec!["a b", "b c"]),
        (10, vec!["a b", "b c", "c d"]),
    ];
    for (num_bigrams, expected) in cases {
        assert_eq!(get_bigrams(num_bigrams, &words), expected, "{num_bigrams}");
    }
}
