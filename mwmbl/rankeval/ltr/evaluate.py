"""
Evaluate a learning to rank dataset.
"""
import pickle
from argparse import ArgumentParser

import numpy as np
import pandas as pd
from mwmbl.tinysearchengine.ltr import ThresholdPredictor, FeatureExtractor, RankingPredictor
from scipy.stats import sem
from sklearn.base import clone
from sklearn.dummy import DummyRegressor
from sklearn.metrics import ndcg_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.tree import DecisionTreeClassifier
from xgboost import XGBClassifier, XGBRegressor, XGBRanker

from mwmbl.rankeval.evaluation.evaluate import CLICK_PROPORTIONS
from mwmbl.rankeval.ltr.baseline import RandomRegressor
from mwmbl.rankeval.paths import LEARNING_TO_RANK_DATASET_PATH, MODEL_PATH

PREDICTORS = {
    'random': RandomRegressor(),
    'constant': DummyRegressor(),
    'decision_tree': make_pipeline(FeatureExtractor(), ThresholdPredictor(0.0, DecisionTreeClassifier())),
    'xgb': make_pipeline(FeatureExtractor(), ThresholdPredictor(0.0, XGBClassifier(scale_pos_weight=0.1, reg_lambda=2))),
    'xgb_limit_terms': RankingPredictor(FeatureExtractor(), ThresholdPredictor(0.0, XGBClassifier(scale_pos_weight=0.1, reg_lambda=2))),
    'xgb_regressor': make_pipeline(FeatureExtractor(), XGBRegressor(objective="reg:pseudohubererror")),
    'xgb_ranker': make_pipeline(FeatureExtractor(), XGBRanker(objective="rank:ndcg", reg_lambda=2)),
}


def print_feature_importances(predictor_name: str, model):
    """Print top feature importances for XGBoost models."""
    xgb_model = None
    if hasattr(model, 'steps'):
        last_step = model.steps[-1][1]
        if isinstance(last_step, ThresholdPredictor):
            xgb_model = last_step.classifier
        elif hasattr(last_step, 'feature_importances_'):
            xgb_model = last_step
    elif isinstance(model, RankingPredictor):
        inner = model.model
        if isinstance(inner, ThresholdPredictor):
            xgb_model = inner.classifier
        elif hasattr(inner, 'feature_importances_'):
            xgb_model = inner

    if xgb_model is None or not hasattr(xgb_model, 'feature_importances_'):
        return

    importances = xgb_model.feature_importances_
    feature_names = xgb_model.get_booster().feature_names or [f"f{i}" for i in range(len(importances))]

    ranked = sorted(zip(feature_names, importances), key=lambda x: x[1], reverse=True)
    print(f"\nTop features for predictor '{predictor_name}':")
    for name, importance in ranked:
        print(f"  {name}: {importance:.4f}")


def get_discount(rank: float):
    if np.isnan(rank):
        return 0.0
    if rank >= len(CLICK_PROPORTIONS):
        return CLICK_PROPORTIONS[-1]
    return CLICK_PROPORTIONS[int(rank)]


def run():
    parser = ArgumentParser()
    parser.add_argument('--predictor', required=True, choices=sorted(PREDICTORS))
    parser.add_argument("--binary-labels", required=False, action="store_true")
    parser.add_argument('--note', required=True)

    args = parser.parse_args()

    predictor = PREDICTORS[args.predictor]

    dataset = pd.read_csv(LEARNING_TO_RANK_DATASET_PATH, lineterminator='\n')
    if args.binary_labels:
        dataset['gold_discount'] = dataset['gold_standard_rank'].apply(lambda x: 1 if x > 0 else 0)
    else:
        dataset['gold_discount'] = dataset['gold_standard_rank'].apply(get_discount)

    print("Gold standard", dataset['gold_discount'])

    X = dataset[['query', 'url', 'title', 'extract', 'score']]
    X['title'].fillna('', inplace=True)
    X['extract'].fillna('', inplace=True)
    y = dataset['gold_discount']
    query_id, query_index = dataset['query'].factorize()
    groups = dataset['query']

    cross_validator = GroupKFold(n_splits=3)

    splits = cross_validator.split(X, y, groups)

    scores = []
    for train, test in splits:
        model = clone(predictor)
        if args.predictor == 'xgb_ranker':
            model.fit(X.iloc[train], y.iloc[train], xgbranker__qid=query_id[train])
        else:
            model.fit(X.iloc[train], y.iloc[train])

        predictions = model.predict(X.iloc[test])

        test_dataset = dataset.iloc[test].copy()
        test_dataset['prediction'] = predictions
        for query, rankings in test_dataset.groupby('query'):
            if len(rankings) == 1:
                continue

            rankings_gold_discount = rankings['gold_discount'].tolist()
            rankings_prediction = rankings['prediction'].tolist()
            print("Rankings gold discount", rankings_gold_discount)
            print("Rankings prediction", rankings_prediction)
            score = ndcg_score([rankings_gold_discount], [rankings_prediction])
            scores.append(score)

    print("scores:", scores)
    print("mean_score:", np.mean(scores))
    print("stderr_score:", sem(scores))

    final_model = clone(predictor)

    if args.predictor == 'xgb_ranker':
        final_model.fit(X, y, xgbranker__qid=query_id)
    else:
        final_model.fit(X, y)

    with open(MODEL_PATH, 'wb') as output_file:
        pickle.dump(final_model, output_file)

    print_feature_importances(args.predictor, final_model)


if __name__ == '__main__':
    run()
