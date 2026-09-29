"""End-to-end en-gb evaluation of the objective experiment's arms, judged by Haiku.

The en-gb queries of `combined-search-haiku-eval.md` share no query with the 849 the models
are trained on. For each query this retrieves the index's candidates once (`RemoteIndex`
against api.mwmbl.org), adds Staan's en-gb results from `rows-0.05.json` at their ranks, and
ranks the pool with Combined Search's own ranker (`CombinedLTRRanker` + MMR) once per arm:
the shipped model, and each arm of `objective_experiment.py` retrained on all 849 queries.

A retrained booster is wrapped so that the ranker sees what it expects: the sigmoid of the
booster's margin (always > 0), and exactly 0 for what the majority-terms filter drops.
That keeps `LTRRanker`'s `predictions > 0` mask a filter and nothing else, whatever the
objective.

    rank         rank the pool with every arm -> engb_arms.json
    batches      UK pass-3 batches for top-ten URLs no judgment covers, plus anchors
    consolidate  judge output -> pass3_engb_arms.jsonl
    report       NDCG@10 of every arm against the union of all en-gb pass-3 judgments

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
import numpy as np
import pandas as pd
import xgboost as xgb
from django.conf import settings

import mwmbl_rank

sys.path.insert(0, "scripts")
sys.path.insert(0, "scripts/combined_ltr_labels")
from llm_relabel_pass3_judge import EXTRACT_CHARS, JUDGE_PROMPT
from objective_experiment import MATCH_TERMS, NUM_TERMS, OBJECTIVES, features, load, train

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.indexer import Document, DocumentSource
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker
from mwmbl.tinysearchengine.staan import staan_score

ENGB = Path("devdata/combined_providers_eval/engb")
JUDGMENTS = Path("devdata/combined_providers_eval/haiku/pass3_engb.jsonl")
LABELS = Path("devdata/combined_ltr_labels")
SHIPPED = "shipped"
ANCHORS = 2
CANDIDATES_PER_BATCH = 250
LINE = re.compile(r"^\s*(\d+)\s*\|\|\s*(\d+)\s*\|\|\s*(\d+)\s*\|\|\s*(\d+)\s*$")


@dataclass(frozen=True)
class Run:
    """One end-to-end run's files, and the arms its report compares every arm against."""

    arms: Path
    judgments: Path
    work: Path
    baselines: tuple[str, ...]


OBJECTIVE_RUN = Run(
    arms=LABELS / "engb_arms.json",
    judgments=LABELS / "pass3_engb_arms.jsonl",
    work=Path(os.environ.get("HAIKU_WORK_DIR", "devdata/combined_ltr_labels/engb_work")),
    baselines=(SHIPPED, "binary"),
)
# Every run's new judgments, so that no run asks Haiku about a URL another run had judged.
RUN_JUDGMENTS = [
    LABELS / "pass3_engb_arms.jsonl",
    LABELS / "pass3_engb_minilm.jsonl",
    LABELS / "pass3_engb_domain.jsonl",
    LABELS / "pass3_engb_domain_cc.jsonl",
    LABELS / "pass3_engb_cascade.jsonl",
    LABELS / "pass3_engb_staan.jsonl",
]


class BoosterModel:
    def __init__(self, booster: xgb.Booster):
        self.booster = booster

    def features(self, records: list[dict]) -> np.ndarray:
        return np.array(mwmbl_rank.RustXGBPipeline.extract_features(records, True), dtype=np.float32)

    def predict(self, records: list[dict]) -> np.ndarray:
        feats = self.features(records)
        margins = self.booster.predict(xgb.DMatrix(feats), output_margin=True)
        from_staan = np.array([record["from_staan"] for record in records])
        kept = from_staan | (feats[:, MATCH_TERMS] > feats[:, NUM_TERMS] / 2)
        return np.where(kept, 1 / (1 + np.exp(-margins)), 0.0)


def staan_documents(row: dict, text: dict) -> list[Document]:
    """Staan's results at Staan's ranks. `row["staan_urls"]` holds the same URLs sorted
    alphabetically; runs before 2026-09-28 took their ranks from it by mistake."""
    return [
        Document(
            title=text[url][0],
            url=url,
            extract=text[url][1],
            score=staan_score(rank),
            term=row["query"],
            source=DocumentSource.STAAN,
        )
        for rank, url in enumerate(row["lists"]["staan"][:10])
    ]


def rank():
    llm, ext, new, _ = load()
    frames = {"llm": llm, "ext": ext, "new": new}
    feats = {name: features(frame) for name, frame in frames.items()}
    models = {SHIPPED: RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))}
    for objective in OBJECTIVES:
        for with_new in (False, True):
            names = ["llm", "ext", "new"] if with_new else ["llm", "ext"]
            frame = pd.concat([frames[n] for n in names], ignore_index=True)
            booster = train(objective, frame, np.concatenate([feats[n] for n in names]))
            models[f"{objective}{'+new' if with_new else ''}"] = BoosterModel(booster)
            print("trained", objective, with_new, flush=True)
    rank_all(models, OBJECTIVE_RUN.arms)


def rank_all(models: dict, path: Path, keep: int = 10, no_mmr: tuple[str, ...] = ()):
    """Ranks one fresh retrieval per en-gb query with every model, and writes the top `keep`.

    Each arm in `no_mmr` is also ranked without MMR's diversity re-ranking, as "<arm>, no MMR".
    """
    index = RemoteIndex()
    rankers = {arm: MMRRanker(CombinedLTRRanker(index, DummyCompleter(), model)) for arm, model in models.items()}
    rankers.update({f"{arm}, no MMR": CombinedLTRRanker(index, DummyCompleter(), models[arm]) for arm in no_mmr})
    retriever = rankers[SHIPPED]
    rows = json.loads((ENGB / "rows-0.05.json").read_text())
    text = json.loads((ENGB / "pool_text.json").read_text())
    out = {}
    for i, row in enumerate(rows):
        query = row["query"]
        retrieval = retriever.retrieve(query)
        lists, pages = {}, {}
        for arm, ranker in rankers.items():
            results = ranker.search_retrieved(retrieval, staan_documents(row, text[query]))[:keep]
            lists[arm] = [page.url for page in results]
            pages.update({page.url: [page.title, page.extract] for page in results})
        out[query] = {"lists": lists, "pages": pages}
        if i % 25 == 0:
            print(i, query, flush=True)
    path.write_text(json.dumps(out))


def judged(exclude: Path | None = None) -> dict[str, dict[str, dict]]:
    grades: dict[str, dict[str, dict]] = {}
    paths = [JUDGMENTS] + [path for path in RUN_JUDGMENTS if path.exists() and path != exclude]
    for path in paths:
        for line in open(path):
            record = json.loads(line)
            if not record.get("anchor"):
                grades.setdefault(record["query"], {})[record["url"]] = record
    return grades


def uk_prompt() -> str:
    """Pass 3's prompt, told the searcher is in the UK."""
    prompt = JUDGE_PROMPT.replace(
        "You are a careful search-quality judge for Mwmbl, an independent non-profit\nsearch engine.",
        "You are a careful search-quality judge for Mwmbl, an independent non-profit\nsearch engine. "
        "The searcher is in the United Kingdom.",
    )
    assert prompt != JUDGE_PROMPT
    return prompt


def batches(run: Run):
    arms = json.loads(run.arms.read_text())
    grades = judged()
    prompt = uk_prompt()
    manifest, blocks, next_id = {}, [], 1
    for i, (query, entry) in enumerate(sorted(arms.items())):
        shown = list(dict.fromkeys(url for urls in entry["lists"].values() for url in urls))
        new = [url for url in shown if url not in grades.get(query, {})]
        if not new:
            continue
        rng = random.Random(i)
        anchors = [url for url in shown if url in grades.get(query, {})]
        rng.shuffle(anchors)
        chosen = [(url, False) for url in new] + [(url, True) for url in anchors[:ANCHORS]]
        rng.shuffle(chosen)
        lines = [f"\n=== QUERY: {query}\n=== INTENT: (not given - infer the most likely intent)"]
        for url, is_anchor in chosen:
            manifest[next_id] = [query, url, is_anchor]
            title, extract = entry["pages"][url]
            extract = (extract or "").replace("\n", " ")[:EXTRACT_CHARS]
            lines.append(f"{next_id}. {title or '(no title)'} — {url}\n   {extract}")
            next_id += 1
        blocks.append((len(chosen), "\n".join(lines)))

    run.work.mkdir(parents=True, exist_ok=True)
    groups: list[list[str]] = [[]]
    size = 0
    for count, block in blocks:
        if size + count > CANDIDATES_PER_BATCH and groups[-1]:
            groups.append([])
            size = 0
        groups[-1].append(block)
        size += count
    for b, group in enumerate(groups):
        (run.work / f"batch_{b:02d}.txt").write_text(prompt + "\n".join(group) + "\n")
    (run.work / "manifest.json").write_text(json.dumps(manifest))
    print(f"{len(blocks)} queries, {len(manifest)} candidates in {len(groups)} batches")


def consolidate(run: Run):
    manifest = {int(k): v for k, v in json.loads((run.work / "manifest.json").read_text()).items()}
    grades, judges = {}, {}
    for path in sorted(run.work.glob("out_*.txt")):
        for line in path.read_text().splitlines():
            match = LINE.match(line)
            if not match:
                continue
            cid, relevance, ethos, overall = map(int, match.groups())
            assert cid in manifest and cid not in grades, f"{path.name}: bad or repeated id {cid}"
            assert relevance <= 3 and ethos <= 3 and overall <= 10, f"{path.name}: out of range: {line}"
            grades[cid] = (relevance, ethos, overall)
            judges[cid] = path.stem.removeprefix("out_")
    missing = sorted(set(manifest) - set(grades))
    assert not missing, f"{len(missing)} ids ungraded, first {missing[:10]}"
    original = judged(exclude=run.judgments)
    drift = []
    with open(run.judgments, "w") as f:
        for cid in sorted(manifest):
            query, url, is_anchor = manifest[cid]
            relevance, ethos, overall = grades[cid]
            record = {"query": query, "url": url, "relevance": relevance, "ethos": ethos, "overall": overall}
            record["judge"] = judges[cid]
            if is_anchor:
                record["anchor"] = True
                drift.append(overall - original[query][url]["overall"])
            f.write(json.dumps(record) + "\n")
    print(f"{len(grades) - len(drift)} new, {len(drift)} anchors; anchor overall drift {np.mean(drift):+.2f}")


def report(run: Run):
    arms = json.loads(run.arms.read_text())
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    grades = judged()
    discounts = 1 / np.log2(np.arange(2, 12))
    scores: dict[str, list[float]] = {}
    weak: dict[str, list[float]] = {}
    for query, entry in sorted(arms.items()):
        lists = dict(entry["lists"])
        lists["brave"] = rows[query]["lists"]["brave"][:10]
        query_grades = grades.get(query, {})
        if not all(url in query_grades for urls in lists.values() for url in urls):
            continue
        ideal = np.sort([g["overall"] for g in query_grades.values()])[::-1][:10]
        ideal_dcg = float(np.sum(ideal * discounts[: len(ideal)]))
        for arm, urls in lists.items():
            gains = np.array([query_grades[url]["overall"] for url in urls], dtype=float)
            scores.setdefault(arm, []).append(float(np.sum(gains * discounts[: len(gains)])) / ideal_dcg)
            weak.setdefault(arm, []).append(float(np.mean(gains <= 3)) if len(gains) else 0.0)

    rng = np.random.default_rng(0)
    print(f"\n## en-gb, pass-3 overall NDCG@10 ({len(scores[SHIPPED])} queries)\n")
    print("| Arm | NDCG@10 | " + " | ".join(f"vs {base}, 95% CI" for base in run.baselines) + " | weak in top 10 |")
    print("|---|---|" + "---|" * len(run.baselines) + "---|")
    for arm, values in scores.items():
        values = np.array(values)
        cells = []
        for base in run.baselines:
            diff = values - np.array(scores[base])
            means = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(2000)]
            cells.append(f"{diff.mean():+.3f} [{np.percentile(means, 2.5):+.3f}, {np.percentile(means, 97.5):+.3f}]")
        print(f"| {arm} | {values.mean():.3f} | {cells[0]} | {cells[1]} | {np.mean(weak[arm]):.1%} |")


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "rank":
        rank()
    else:
        {"batches": batches, "consolidate": consolidate, "report": report}[command](OBJECTIVE_RUN)
