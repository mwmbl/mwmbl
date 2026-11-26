use mwmbl_core::wiki::get_wiki_score;

#[test]
fn wiki_score_unknown() {
    let score = get_wiki_score("https://en.wikipedia.org/wiki/Totally_Unknown_Article_XYZ");
    assert_eq!(score, 0.0);
}

#[test]
fn wiki_score_is_the_normalised_count_of_the_last_path_segment() {
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
