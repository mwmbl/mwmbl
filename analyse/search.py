import logging
import sys

from mwmbl.indexer.paths import INDEX_PATH
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import HeuristicRanker

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)


def run():
    with TinyIndex(Document, INDEX_PATH) as tiny_index:
        completer = Completer()
        ranker = HeuristicRanker(tiny_index, completer)
        items = ranker.search('jasper fforde')
        if items:
            for item in items:
                print("Items", item)


if __name__ == '__main__':
    run()
