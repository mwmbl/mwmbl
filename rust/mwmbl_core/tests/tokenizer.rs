use mwmbl_core::tokenizer::{get_bigrams, tokenize};

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

#[test]
fn tokens_lose_their_sentence_punctuation_but_not_their_own() {
    let cases = [
        (
            "Wikipedia, the (free) encyclopedia.",
            vec!["wikipedia", "the", "free", "encyclopedia"],
        ),
        (
            "\"C++,\" and (C#) or C#?",
            vec!["c++", "and", "c#", "or", "c#"],
        ),
        (
            "git config --global user.name",
            vec!["git", "config", "--global", "user.name"],
        ),
        (
            ".NET - Node.js -- U.S.",
            vec![".net", "-", "node.js", "--", "u.s"],
        ),
        (
            "std::vector re: «ça» “quoted”",
            vec!["std::vector", "re", "ça", "quoted"],
        ),
        ("... ?! ()", vec!["...", "?!", "()"]),
        ("cut…", vec![]),
        ("a\u{1c}b c\u{1f}d", vec!["a", "b", "c", "d"]),
    ];
    for (text, expected) in cases {
        assert_eq!(tokenize(text), expected, "{text}");
    }
}
