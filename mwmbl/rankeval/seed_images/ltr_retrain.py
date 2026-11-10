"""Does retraining standard search's LTR with the Commons image rows help, and how many images should a list show? (#610)

AGENTS.md's two evaluation steps over ltr_data.py's dataset. `cv` cross-validates each arm over the LLM-labelled
queries (five folds by query, pass-3 NDCG@10), on the whole test fold and on the fold without its image rows. The
deployed model was trained on all of these queries, so `today`, its recipe retrained per fold, is the fair baseline.
Labels are `overall`, which the image rows' ethos (3 for almost all) and grading beside pictures both lift, and
`relevance`, which neither lifts much. `search` runs standard search's ranker (LTR + MMR) on the en-gb queries over
production's own pages with images overlaid as ltr_data.py overlays them. Run from the repo root with
DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and DATABASE_URL; `search` also needs EXTERNAL_CACHE_ENABLED=false and
PYTHONHASHSEED=0.

    cv                           -> ltr/cv.json
    train ARM                    -> ltr/model-ARM.xgb, trained on every query
    search NAME MODEL IMAGES CAP -> ltr/holistic/lists_NAME.json; IMAGES is train (the train queries' images), all
                                    (the en-gb queries' own too) or none; CAP is the most images kept, or none
    batches X Y                  holistic-judge batches for lists_X against lists_Y -> ltr/holistic/X_vs_Y
"""

import json
import os
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path

import django
import numpy as np
import pandas as pd
from django.conf import settings
from scipy.stats import sem
from sklearn.metrics import ndcg_score

# ltr_data and llm_experiment reach Django models, so the registry must be up first.
django.setup()

from mwmbl.imagesearch.types import SourceImage  # noqa: E402
from mwmbl.rankeval.evaluation import holistic_judge  # noqa: E402
from mwmbl.rankeval.ltr.llm_experiment import (  # noqa: E402
    FEATURE_COLUMNS,
    drop_pool,
    load_curation_frame,
    load_human_pairs,
    pair_accuracy,
    train,
)
from mwmbl.rankeval.seed_images.gate_data import jev_safe_candidates  # noqa: E402
from mwmbl.rankeval.seed_images.indexing import (  # noqa: E402
    approved_train_images,
    engb_queries,
    fetch_wiki,
    is_image,
    wiki_documents,
)
from mwmbl.rankeval.seed_images.ltr_data import (  # noqa: E402
    DATASET_PATH,
    IMAGE_POOL,
    LTR_DIR,
    image_term_documents,
    page_index,
    remote_page,
    stored_page,
)
from mwmbl.rankeval.seed_images.queries import NUM_RESULTS, load_queries  # noqa: E402
from mwmbl.tinysearchengine.completer import Completer  # noqa: E402
from mwmbl.tinysearchengine.indexer import Document  # noqa: E402
from mwmbl.tinysearchengine.ltr import RustXGBPipeline  # noqa: E402
from mwmbl.tinysearchengine.ltr_rank import LTRRanker  # noqa: E402
from mwmbl.tinysearchengine.mmr_rank import MMRRanker  # noqa: E402

CV_PATH = LTR_DIR / "cv.json"
HOLISTIC_DIR = LTR_DIR / "holistic"
NUM_FOLDS = 5
SPLIT_SEED = 42
# The deployed model's recipe (commit 76b58d2): scale_pos_weight 0.1, reg_lambda 2, 100 rounds, curation at 0.5.
SCALE_POS_WEIGHT = 0.1
REG_LAMBDA = 2.0
NUM_ROUNDS = 100
CURATION_WEIGHT = 0.5
WEAK_NEGATIVE_WEIGHT = 0.25


@dataclass(frozen=True)
class Arm:
    with_images: bool
    label: str
    threshold: int


# Relevance >= 2 is the relevance cut nearest overall >= 6: 37% of web rows pass it, against 31%.
ARMS = {
    "today": Arm(False, "overall", 6),
    "images_overall": Arm(True, "overall", 6),
    "today_relevance": Arm(False, "relevance", 2),
    "images_relevance": Arm(True, "relevance", 2),
}


def normalised(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(
        qnorm=frame["query"].str.lower().str.strip(),
        title=frame["title"].fillna(""),
        extract=frame["extract"].fillna(""),
        score=frame["score"].fillna(0.0),
        is_image=frame["pools"] == IMAGE_POOL,
    )


def folds(keys: list[str]) -> dict[str, int]:
    shuffled = np.sort(np.unique(keys))
    np.random.default_rng(SPLIT_SEED).shuffle(shuffled)
    return {key: position % NUM_FOLDS for position, key in enumerate(shuffled)}


def training_frame(llm: pd.DataFrame, curation: pd.DataFrame, excluded: set[str], arm: Arm) -> pd.DataFrame:
    """The arm's rows of the queries not excluded, plus the curation rows no LLM row covers, as deployed."""
    rows = llm if arm.with_images else drop_pool(llm, IMAGE_POOL)
    rows = rows[~rows["qnorm"].isin(excluded)]
    rows = rows.assign(label=(rows[arm.label] >= arm.threshold).astype(float), weight=1.0)
    covered = set(zip(rows["qnorm"], rows["url"]))
    curation = curation[~curation["qnorm"].isin(excluded)]
    uncovered = np.array([pair not in covered for pair in zip(curation["qnorm"], curation["url"])], dtype=bool)
    curation = curation.loc[uncovered]
    curation = curation.assign(weight=curation["weight"] * CURATION_WEIGHT)
    return pd.concat([rows, curation], ignore_index=True)[FEATURE_COLUMNS + ["label", "weight"]]


def per_query_ndcg(test: pd.DataFrame, gain: str) -> dict[str, float]:
    """NDCG@10 of the `prediction` column for each query that has something to rank and a gain above zero."""
    scores = {}
    for query, rows in test.groupby("qnorm"):
        if len(rows) > 1 and rows[gain].sum() > 0:
            scores[query] = ndcg_score([rows[gain].tolist()], [rows["prediction"].tolist()], k=10)
    return scores


def image_placement(test: pd.DataFrame) -> dict[str, float]:
    """Where the images go, on the queries with any: how many reach the top ten, and how often one is first."""
    queries = [rows.sort_values("prediction", ascending=False) for _, rows in test.groupby("qnorm")]
    with_images = [rows for rows in queries if rows["is_image"].any()]
    return {
        "image_queries": len(with_images),
        "images_in_top_ten": float(np.mean([rows["is_image"].head(NUM_RESULTS).sum() for rows in with_images])),
        "image_first": float(np.mean([rows["is_image"].iloc[0] for rows in with_images])),
    }


def scored(model, test: pd.DataFrame) -> pd.DataFrame:
    return test.assign(prediction=model.predict(test[FEATURE_COLUMNS]))


def arm_metrics(test: pd.DataFrame) -> dict:
    web = test[~test["is_image"]]
    return {
        "ndcg@10": per_query_ndcg(test, "overall"),
        "ndcg@10 web only": per_query_ndcg(web, "overall"),
        "ndcg@10 relevance gain": per_query_ndcg(test, "relevance"),
    }


def summary(per_query: dict[str, dict[str, float]], baseline: dict[str, dict[str, float]]) -> dict:
    """Each metric's mean, and its paired difference from the baseline arm with a standard error."""
    result = {}
    for metric, scores in per_query.items():
        shared = sorted(set(scores) & set(baseline[metric]))
        differences = [scores[query] - baseline[metric][query] for query in shared]
        result[metric] = {
            "mean": float(np.mean(list(scores.values()))),
            "vs_today": float(np.mean(differences)),
            "vs_today_sem": float(sem(differences)),
        }
    return result


def cross_validate() -> None:
    llm = normalised(pd.read_csv(DATASET_PATH, index_col=0, lineterminator="\n"))
    curation = load_curation_frame(WEAK_NEGATIVE_WEIGHT)
    human_pairs = load_human_pairs()
    llm_folds = folds(llm["qnorm"].tolist())
    human_folds = folds([pair["qnorm"] for pair in human_pairs])
    deployed = RustXGBPipeline.from_model_path(str(settings.RUST_MODEL_PATH))
    names = ["deployed", *ARMS]
    per_query = {name: {} for name in names}
    placement = {name: [] for name in names}
    pair_accuracies = {name: [] for name in names}
    for fold in range(NUM_FOLDS):
        test_queries = {query for query, number in llm_folds.items() if number == fold}
        human_test_queries = {query for query, number in human_folds.items() if number == fold}
        human_test_pairs = [pair for pair in human_pairs if pair["qnorm"] in human_test_queries]
        test = llm[llm["qnorm"].isin(test_queries)]
        for name in names:
            if name == "deployed":
                model = deployed
            else:
                frame = training_frame(llm, curation, test_queries | human_test_queries, ARMS[name])
                model = train(frame, SCALE_POS_WEIGHT, REG_LAMBDA, NUM_ROUNDS)
            predicted = scored(model, test)
            for metric, scores in arm_metrics(predicted).items():
                per_query[name].setdefault(metric, {}).update(scores)
            placement[name].append(image_placement(predicted))
            pair_accuracies[name].append(pair_accuracy(model, human_test_pairs)[0])
            print(f"fold {fold} {name}: done")
    results = {}
    for name in names:
        results[name] = summary(per_query[name], per_query["today"]) | {
            "human pair accuracy": float(np.mean(pair_accuracies[name])),
            "images in top ten": float(np.mean([p["images_in_top_ten"] for p in placement[name]])),
            "image first": float(np.mean([p["image_first"] for p in placement[name]])),
        }
        print(name, json.dumps(results[name]))
    CV_PATH.write_text(json.dumps(results, indent=1))


def train_arm(name: str) -> None:
    llm = normalised(pd.read_csv(DATASET_PATH, index_col=0, lineterminator="\n"))
    frame = training_frame(llm, load_curation_frame(WEAK_NEGATIVE_WEIGHT), set(), ARMS[name])
    model = train(frame, SCALE_POS_WEIGHT, REG_LAMBDA, NUM_ROUNDS)
    model.save_model(str(LTR_DIR / f"model-{name}.xgb"))


class OverlaidIndex:
    """Production's pages, read whole from api.mwmbl.org, with the images indexing would send each page written in."""

    def __init__(self, image_pages: dict[int, list[Document]]):
        self.image_pages = image_pages
        self.pages: dict[int, list[Document]] = {}

    def retrieve(self, term: str) -> list[Document]:
        number = page_index(term)
        if number not in self.pages:
            existing = remote_page(number)
            incoming = self.image_pages.get(number, [])
            self.pages[number] = stored_page(existing, incoming) if incoming else existing
        return [document for document in self.pages[number] if document.term == term]


def capped(results: list[Document], cap: int | None) -> list[Document]:
    """The results with every image after the first `cap` left out, so the web results below them move up."""
    if cap is None:
        return results
    image_positions = [position for position, result in enumerate(results) if is_image(result.url)]
    dropped = set(image_positions[cap:])
    return [result for position, result in enumerate(results) if position not in dropped]


def approved_images(image_set: str) -> list[SourceImage]:
    """The Jev-safe candidates of the image set's queries, each once."""
    if image_set == "train":
        return approved_train_images()
    safe = jev_safe_candidates()
    by_url = {image.page_url: image for query in load_queries() for image in safe.get(query, [])}
    return list(by_url.values())


def search(name: str, model_path: Path, image_set: str, cap: str) -> None:
    assert os.environ.get("PYTHONHASHSEED") == "0", "set PYTHONHASHSEED=0: with a random seed, tied results swap places"
    queries = engb_queries()
    wiki = fetch_wiki(queries)
    image_pages = {} if image_set == "none" else image_term_documents(approved_images(image_set))
    image_cap = None if cap == "none" else int(cap)
    model = RustXGBPipeline.from_model_path(str(model_path))
    ranker = MMRRanker(LTRRanker(OverlaidIndex(image_pages), Completer(), model, include_wiki=False))
    lists = {}
    for query in queries:
        # Passing Wikipedia's results in is what include_wiki would do, from the one fetch every arm shares.
        results = capped(ranker.search(query, wiki_documents(wiki[query])), image_cap)[:NUM_RESULTS]
        # The holistic judge marks a single picture by "image", and shows the extract the index holds.
        lists[query] = [
            {"url": d.url, "title": d.title, "extract": d.extract} | ({"image": True} if is_image(d.url) else {})
            for d in results
        ]
    HOLISTIC_DIR.mkdir(parents=True, exist_ok=True)
    (HOLISTIC_DIR / f"lists_{name}.json").write_text(json.dumps(lists))
    shown = [sum("image" in r for r in results) for results in lists.values()]
    print(f"{name}: {sum(map(bool, shown))} queries show {sum(shown)} images, at most {max(shown)} in one list")


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["cv", "train", "search", "batches"])
    parser.add_argument("extra", nargs="*")
    args = parser.parse_args()

    if args.command == "cv":
        cross_validate()
    elif args.command == "train":
        train_arm(args.extra[0])
    elif args.command == "search":
        name, model_path, image_set, cap = args.extra
        search(name, Path(model_path), image_set, cap)
    else:
        x, y = args.extra
        holistic_judge.batches(
            HOLISTIC_DIR / f"lists_{x}.json", HOLISTIC_DIR / f"lists_{y}.json", HOLISTIC_DIR / f"{x}_vs_{y}"
        )


if __name__ == "__main__":
    main()
