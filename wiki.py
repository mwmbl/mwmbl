"""
Index Wikipedia
"""
import bz2
from xml.dom import minidom
from xml.etree import ElementTree
from xml.etree.ElementTree import XMLParser

from mediawiki_parser import preprocessor, text

import wikitextparser as wtp

from paths import WIKI_DATA_PATH

TEXT_TAGS = ['mediawiki', 'page', 'revision', 'text']


class WikiIndexer:
    def __init__(self):
        self.tags = []
        self.current_data = ''

        self.wiki_preprocessor = preprocessor.make_parser({})
        self.parser = text.make_parser()


    def start(self, tag, attr):
        tagname = tag.split('}')[-1]
        self.tags.append(tagname)
        # print("Start", self.tags)

    def end(self, tag):
        if self.tags == TEXT_TAGS:
            self.handle_data(self.current_data)
            self.current_data = ''
        self.tags.pop()
        # print("End", tag)

    def data(self, data):
        # print("Data", self.tags)
        if self.tags == TEXT_TAGS:
            self.current_data += data
        pass

    def close(self):
        pass

    def handle_data(self, data):
        preprocessed_text = self.wiki_preprocessor.parse(data)
        output = self.parser.parse(preprocessed_text.leaves())

        print("Data", output)


def index_wiki():
    target = WikiIndexer()
    parser = XMLParser(target=target)
    with bz2.open(WIKI_DATA_PATH, 'rt') as wiki_file:
        for line in wiki_file:
            parser.feed(line)


if __name__ == '__main__':
    index_wiki()
