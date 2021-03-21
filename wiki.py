"""
Index Wikipedia
"""
import bz2
import gzip
from xml.etree.ElementTree import XMLParser

from mediawiki_parser import preprocessor, text
from spacy.lang.en import English

from index import tokenize, index
from paths import WIKI_DATA_PATH, WIKI_TITLES_PATH

TEXT_TAGS = ['mediawiki', 'page', 'revision', 'text']


def index_wiki():
    nlp = English()
    indexed = 0
    with gzip.open(WIKI_TITLES_PATH, 'rt') as wiki_titles_file:
        wiki_titles_file.readline()
        for title in wiki_titles_file:
            title_cleaned = title.replace('_', ' ')
            tokens = tokenize(nlp, title_cleaned)

            if len(tokens) > 0:
                indexed += 1
                url = 'https://en.wikipedia.org/wiki/' + title
                index(tokens, url, title_cleaned)

                if indexed % 1000 == 0:
                    print("Indexed", indexed)


if __name__ == '__main__':
    index_wiki()
