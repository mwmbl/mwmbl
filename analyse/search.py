import logging
import sys
from itertools import islice

from mwmbl.indexer.paths import INDEX_PATH
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)


def clean(sequence):
    return ''.join(x['value'] for x in sequence)


def run():
    with TinyIndex(Document, INDEX_PATH) as tiny_index:
        completer = Completer()
        ranker = HeuristicRanker(tiny_index, completer)
        items = ranker.search('jasper fforde')
        print()
        if items:
            for i, item in enumerate(islice(items, 10)):
                print(f"{i + 1}. {item['url']}")
                print(clean(item['title']))
                print(clean(item['extract']))
                print()


if __name__ == '__main__':
    run()
