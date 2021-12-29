import gzip
from glob import glob

import bs4
from spacy.lang.en import English

from .index import tokenize
from mwmbl.tinysearchengine.indexer import TinyIndexer, NUM_PAGES, PAGE_SIZE
from .paths import INDEX_PATH, CRAWL_GLOB


def run():
    # TODO: item_factory argument is unfilled.
    indexer = TinyIndexer(INDEX_PATH, NUM_PAGES, PAGE_SIZE)
    indexer.create_if_not_exists()
    nlp = English()
    for path in glob(CRAWL_GLOB):
        print("Path", path)
        with gzip.open(path, 'rt') as html_file:
            url = html_file.readline().strip()
            content = html_file.read()

        if indexer.document_indexed(url):
            print("Page exists, skipping", url)
            continue

        cleaned_text = clean(content)
        try:
            title = bs4.BeautifulSoup(content, features="lxml").find('title').string
        except AttributeError:
            title = cleaned_text[:80]
        tokens = tokenize(nlp, cleaned_text)
        print("URL", url)
        print("Tokens", tokens)
        print("Title", title)
        indexer.index(tokens, url, title)


if __name__ == '__main__':
    run()


def clean(content):
    text = justext.justext(content, justext.get_stoplist("English"))
    pars = [par.text for par in text if not par.is_boilerplate]
    cleaned_text = ' '.join(pars)
    return cleaned_text