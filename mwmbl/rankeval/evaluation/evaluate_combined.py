"""
Evaluate a ranking model that combines two other models
"""
from mwmbl.rankeval.evaluation.evaluate import evaluate
from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter, MwmblRankingModel
from mwmbl.rankeval.evaluation.evaluate_wiki import WikiModel
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.rank import HeuristicRanker


class CombinedModel:
    def __init__(self, model1, model2, max_results1: int = 5):
        self.model1 = model1
        self.model2 = model2
        self.max_results1 = max_results1

    def predict(self, query):
        results1 = self.model1.predict(query)
        results2 = self.model2.predict(query)
        results1_set = set(results1)
        return results1[:self.max_results1] + [x for x in results2 if x not in results1_set]


def run():
    model1 = WikiModel()
    ranker = HeuristicRanker(RemoteIndex(), DummyCompleter())
    model2 = MwmblRankingModel(ranker)
    combined_ranker = CombinedModel(model1, model2, max_results1=5)
    evaluate(combined_ranker, fraction=0.01)


if __name__ == "__main__":
    run()
