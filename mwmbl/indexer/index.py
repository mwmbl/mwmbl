"""
Create a search index
"""
from collections import Counter
from itertools import islice
from typing import Iterator, Iterable
from urllib.parse import unquote

import pandas as pd

# NUM_PAGES = 8192
# PAGE_SIZE = 512
from mwmbl.tinysearchengine.indexer import TinyIndexer, Document, TokenizedDocument

NUM_INITIAL_TOKENS = 50

HTTP_START = 'http://'
HTTPS_START = 'https://'
BATCH_SIZE = 100


def is_content_token(nlp, token):
    lexeme = nlp.vocab[token.orth]
    return (lexeme.is_alpha or lexeme.is_digit) and not token.is_stop


def tokenize(nlp, cleaned_text):
    tokens = nlp.tokenizer(cleaned_text)
    content_tokens = [token for token in tokens[:NUM_INITIAL_TOKENS]
                      if is_content_token(nlp, token)]
    lowered = {nlp.vocab[token.orth].text.lower() for token in content_tokens}
    return lowered


def prepare_url_for_tokenizing(url: str):
    if url.startswith(HTTP_START):
        url = url[len(HTTP_START):]
    elif url.startswith(HTTPS_START):
        url = url[len(HTTPS_START):]
    for c in '/._':
        if c in url:
            url = url.replace(c, ' ')
    return url


def get_pages(nlp, titles_urls_and_extracts) -> Iterable[TokenizedDocument]:
    for i, (title_cleaned, url, extract) in enumerate(titles_urls_and_extracts):
        title_tokens = tokenize(nlp, title_cleaned)
        prepared_url = prepare_url_for_tokenizing(unquote(url))
        url_tokens = tokenize(nlp, prepared_url)
        extract_tokens = tokenize(nlp, extract)
        print("Extract tokens", extract_tokens)
        tokens = title_tokens | url_tokens | extract_tokens
        yield TokenizedDocument(tokens=list(tokens), url=url, title=title_cleaned, extract=extract)

        if i % 1000 == 0:
            print("Processed", i)


def grouper(n: int, iterator: Iterator):
    while True:
        chunk = tuple(islice(iterator, n))
        if not chunk:
            return
        yield chunk


def index_titles_urls_and_extracts(indexer: TinyIndexer, nlp, titles_urls_and_extracts, terms_path):
    indexer.create_if_not_exists()

    terms = Counter()
    pages = get_pages(nlp, titles_urls_and_extracts)
    for page in pages:
        for token in page.tokens:
            indexer.index(token, Document(url=page.url, title=page.title, extract=page.extract))
        terms.update([t.lower() for t in page.tokens])

    term_df = pd.DataFrame({
        'term': terms.keys(),
        'count': terms.values(),
    })
    term_df.to_csv(terms_path)
