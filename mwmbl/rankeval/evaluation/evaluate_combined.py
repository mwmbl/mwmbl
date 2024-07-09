"""
Evaluate a ranking model that combines two other models
"""
from mwmbl.rankeval.evaluation.evaluate import evaluate
from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter, MwmblRankingModel
from mwmbl.rankeval.evaluation.evaluate_wiki import WikiRanker
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.rank import HeuristicRanker


class CombinedRanker:
    def __init__(self, ranker1, ranker2, max_results1: int = 5):
        self.ranker1 = ranker1
        self.ranker2 = ranker2
        self.max_results1 = max_results1

    def predict(self, query):
        results1 = self.ranker1.predict(query)
        results2 = self.ranker2.predict(query)
        results1_set = set(results1)
        return results1[:self.max_results1] + [x for x in results2 if x not in results1_set]


def run():
    ranker1 = WikiRanker()
    ranker2 = HeuristicRanker(RemoteIndex(), DummyCompleter())
    model2 = MwmblRankingModel(ranker2)
    combined_ranker = CombinedRanker(ranker1, model2, max_results1=5)
    evaluate(combined_ranker, fraction=0.01)


if __name__ == "__main__":
    run()
