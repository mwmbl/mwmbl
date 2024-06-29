from argparse import ArgumentParser

from mwmbl.tinysearchengine.indexer import TinyIndex, Document
from mwmbl.tinysearchengine.rank import Ranker, HeuristicRanker

from mwmbl.rankeval.evaluation.evaluate import RankingModel, evaluate


class MwmblRankingModel(RankingModel):
    def __init__(self, ranker: Ranker):
        self.ranker = ranker

    def predict(self, query: str) -> list[str]:
        results = self.ranker.search(query)
        return [x['url'] for x in results]


class DummyCompleter:
    def complete(self, q):
        return [q]


def run():
    arg_parser = ArgumentParser()
    arg_parser.add_argument('--index', help='Path to the index', required=True)
    arg_parser.add_argument('--note', required=True)

    args = arg_parser.parse_args()

    completer = DummyCompleter()

    with TinyIndex(item_factory=Document, index_path=args.index) as tiny_index:
        # ranker = Ranker(tiny_index, completer)
        ranker = HeuristicRanker(tiny_index, completer)
        # model = pickle.load(open(MODEL_PATH, 'rb'))
        # ranker = LTRRanker(model, tiny_index, completer)
        model = MwmblRankingModel(ranker)
        evaluate(model)


if __name__ == '__main__':
    run()
