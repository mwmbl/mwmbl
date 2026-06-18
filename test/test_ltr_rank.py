from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.ltr_rank import MMR_WINDOW, mmr_rerank


def _doc(title, url, extract=""):
    return Document(title=title, url=url, extract=extract)


def test_mmr_keeps_all_documents():
    # MMR demotes, it never drops: same-domain results must all survive.
    pages = [
        _doc("artoh/kitsas", "https://github.com/artoh/kitsas", "kitsas kirjanpito"),
        _doc("artoh/kitsasdocsy", "https://github.com/artoh/kitsasdocsy", "kitsas documentation"),
        _doc("artoh/kitsashealth", "https://github.com/artoh/kitsashealth", "health check for kitsas"),
    ]
    reranked = mmr_rerank(pages)
    assert {p.url for p in reranked} == {p.url for p in pages}


def test_mmr_demotes_same_domain_below_fresh_domain():
    # Input is in relevance order: two github.com results then a lower-ranked
    # different-domain result. MMR should lift the fresh domain above the second
    # same-domain result, while keeping all three.
    a = _doc("Alpha repo", "https://github.com/x/alpha", "alpha project")
    b = _doc("Beta repo", "https://github.com/x/beta", "beta project")
    c = _doc("Gamma site", "https://example.org/gamma", "gamma encyclopedia entry")
    reranked = mmr_rerank([a, b, c])
    urls = [p.url for p in reranked]
    assert urls[0] == a.url  # most relevant stays first
    assert urls.index(c.url) < urls.index(b.url)  # fresh domain promoted above 2nd github


def test_mmr_preserves_order_without_duplicates():
    # All distinct domains and content: relevance order should be untouched.
    pages = [
        _doc("First", "https://a.com/1", "apples"),
        _doc("Second", "https://b.com/2", "bananas"),
        _doc("Third", "https://c.com/3", "cherries"),
    ]
    assert [p.url for p in mmr_rerank(pages)] == [p.url for p in pages]


def test_mmr_short_lists_are_unchanged():
    pages = [_doc("a", "https://github.com/x/a"), _doc("b", "https://github.com/x/b")]
    assert mmr_rerank(pages) == pages


def test_mmr_caps_work_to_window_and_keeps_tail():
    # Beyond MMR_WINDOW the long tail keeps plain relevance order (bounded cost),
    # and no document is ever dropped.
    pages = [_doc(f"t{i}", f"https://github.com/x/{i}", "same kind of content") for i in range(MMR_WINDOW + 25)]
    reranked = mmr_rerank(pages)
    assert len(reranked) == len(pages)
    assert {p.url for p in reranked} == {p.url for p in pages}
    # The tail past the window is untouched.
    assert reranked[MMR_WINDOW:] == pages[MMR_WINDOW:]
