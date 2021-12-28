"""
Index Wikipedia
"""
import gzip
import html
from urllib.parse import quote

from spacy.lang.en import English

from .index import index_titles_urls_and_extracts
from mwmbl.tinysearchengine.indexer import TinyIndexer, NUM_PAGES, PAGE_SIZE
from .paths import WIKI_TITLES_PATH, INDEX_PATH

TEXT_TAGS = ['mediawiki', 'page', 'revision', 'text']
TITLE_START = '<title>Wikipedia: '
TITLE_END = '</title>\n'


def index_wiki():
    nlp = English()
    with TinyIndexer(INDEX_PATH, NUM_PAGES, PAGE_SIZE) as indexer:
        titles_and_urls = get_wiki_titles_and_urls()
        index_titles_urls_and_extracts(indexer, nlp, titles_and_urls)


def get_wiki_titles_and_urls():
    start_len = len(TITLE_START)
    end_len = len(TITLE_END)
    with gzip.open(WIKI_TITLES_PATH, 'rt') as wiki_titles_file:
        wiki_titles_file.readline()
        for raw_title in wiki_titles_file:
            assert raw_title.startswith(TITLE_START)
            assert raw_title.endswith(TITLE_END)
            title = raw_title[start_len:-end_len]
            unescaped_title = html.unescape(title)
            url = 'https://en.wikipedia.org/wiki/' + quote(unescaped_title.replace(' ', '_'))
            yield unescaped_title, url


if __name__ == '__main__':
    index_wiki()
