from urllib.parse import unquote

import pytest

from mwmbl.indexer.index import prepare_url_for_tokenizing, tokenize_document
from mwmbl.tokenizer import tokenize

# The same cases as rust/mwmbl_core/tests/, which the crawler's tokenizer must agree with.


@pytest.mark.parametrize(
    "text,tokens",
    [
        ("Wikipedia, the (free) encyclopedia.", ["wikipedia", "the", "free", "encyclopedia"]),
        ('"C++," and (C#) or C#?', ["c++", "and", "c#", "or", "c#"]),
        ("git config --global user.name", ["git", "config", "--global", "user.name"]),
        (".NET - Node.js -- U.S.", [".net", "-", "node.js", "--", "u.s"]),
        ("std::vector re: «ça» “quoted”", ["std::vector", "re", "ça", "quoted"]),
        ("... ?! ()", ["...", "?!", "()"]),
        ("cut…", []),
        ("a\x1cb c\x1fd", ["a", "b", "c", "d"]),
    ],
)
def test_tokenize_strips_sentence_punctuation(text, tokens):
    assert tokenize(text) == tokens


@pytest.mark.parametrize(
    "url,prepared",
    [
        ("http://example.com/a-b_c?d=e", "example com a-b c"),
        ("https://http://example.com", "http:  example com"),
        ("HTTP://Upper.case/", "HTTP:  Upper case "),
        ("https://caf%C3%A9.fr/a%20b", "café fr a b"),
        ("https://isocpp.org/wiki/faq/c++", "isocpp org wiki faq c++"),
    ],
)
def test_prepare_url_for_tokenizing(url, prepared):
    assert prepare_url_for_tokenizing(unquote(url)) == prepared


@pytest.mark.parametrize(
    "url,title,extract,expected",
    [
        (
            "https://en.wikipedia.org/wiki/Caf%C3%A9?x=1",
            "Café — one two three four five six seven eight nine ten eleven",
            "A café (coffee shop) is a shop …",
            "a|a café|café|café coffee|café —|coffee|coffee shop|eight|eight nine|en|en wikipedia|"
            "five|five six|four|four five|is|is a|one|one two|org|org wiki|seven|seven eight|shop|"
            "shop is|six|six seven|three|three four|two|two three|wiki|wiki café|wikipedia|"
            "wikipedia org|—|— one",
        ),
        ("https://a.b/", "x", "y z w…", "a|a b|b|x|y"),
    ],
)
def test_tokenize_document(url, title, extract, expected):
    tokens = tokenize_document(url, title, extract, 0).tokens
    assert "|".join(sorted(tokens)) == expected
