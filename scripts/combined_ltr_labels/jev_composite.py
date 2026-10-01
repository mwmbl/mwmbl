"""Jev + Staan rank with composite Jev questions, an index penalty and a per-site cap.

Judged against Brave, `Jev + Staan rank, w=0.15` loses mostly on off-topic results, the wrong
entity or sense of the query, junk from the index and repeated pages from one site, and Jev's
UK-relevance score barely separates the results the judge flagged from the rest. This asks Jev
three Score questions per candidate, in one request per query:

    relevance  the UK-relevance rubric of `scripts/combined_search_haiku/jev_score.py`, 0-3
    entity     is it about the entity or sense the query most likely means? 0-2
    quality    is it a substantive page, or thin, spam or junk? 0-2

and orders each query's pool by

    relevance + ENTITY x entity + QUALITY x quality - STAAN x Staan's position (10 if absent)
              - INDEX if Staan didn't return it

with the weights tuned on the 849 training queries' serving pool (pass-3 NDCG@10), never on
en-gb. A per-site cap then keeps at most MAX_PER_SITE results from one host in the top ten.

    score   composite Jev scores for the training serving pool and the en-gb pool
            -> jev_composite_scores.json
    tune    grid-search the weights on the training queries
    flags   how well each question separates the results the Brave comparison flagged
    arms    the en-gb arms -> engb_jev_composite_arms.json

The holistic comparisons are `holistic_eval.py batches jev-composite`, `jev-cap` and
`jev-composite-brave`.

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import itertools
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from scripts.combined_ltr_labels.engb_eval import ENGB
from scripts.combined_ltr_labels.jev_experiment import NUM_RESULTS
from scripts.combined_ltr_labels.jev_ltr_eval import RANKED, STAGE1, TUNED_BLEND, TUNED_STAAN_WEIGHT
from scripts.combined_ltr_labels.jev_scores import key
from scripts.combined_ltr_labels.objective_experiment import features, kept, load, ndcg_at_10

sys.path.insert(0, "scripts/combined_search_haiku")
from jev_score import LEVELS, call, result_fields

LABELS = Path("devdata/combined_ltr_labels")
SCORES = LABELS / "jev_composite_scores.json"
ARMS = LABELS / "engb_jev_composite_arms.json"
JEV_LTR_ARMS = LABELS / "engb_jev_ltr_arms.json"
BRAVE_JUDGMENTS = LABELS / "holistic_jev_brave_judgments.jsonl"
CACHE = LABELS / "jev_composite_work"
CANDIDATES_PER_REQUEST = 30
NOT_IN_STAAN = 10
MAX_PER_SITE = 2

QUESTIONS = {
    "relevance": (
        "How well does the search result `result` satisfy `query`, for `searcher`?",
        LEVELS,
    ),
    "entity": (
        "Is the search result `result` about the specific thing `query` most likely refers to: the same "
        "business, organisation, person, place, product or work, or the same sense of the words?",
        [
            "Different: about something else that only shares a word or name with the query - another "
            "business, person or place of the same name, another product, or another meaning of the words.",
            "Related: on the same general topic, but not the specific entity or sense the query most likely means.",
            "Same: about exactly the entity, or the sense, that the query most likely means.",
        ],
    ),
    "quality": (
        "Is the search result `result` a substantive page, judging by its title, URL and snippet?",
        [
            "Junk: spam, a content farm, a doorway or SEO page, a scraped or auto-generated page, a page "
            "selling something unrelated, or a broken, empty, login or error page.",
            "Thin: little real content - a stub, a bare listing or directory page, a tag or search page, "
            "or a page that only repeats the query's words.",
            "Substantive: a real page with real content, or the real site or service it names.",
        ],
    ),
}
COLUMNS = tuple(QUESTIONS)

ENTITY_GRID = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5)
QUALITY_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
STAAN_GRID = (0.05, 0.1, 0.15, 0.2, 0.25)
INDEX_GRID = (0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5)

COMPOSITE = "Jev composite + Staan rank"
COMPOSITE_CAP = f"Jev composite + Staan rank, at most {MAX_PER_SITE} per site"


def request(query: str, docs: list[dict]) -> dict:
    questions = {
        f"{column[0]}{i}": {
            "type": "score",
            "instructions": {"result": result_fields(doc), "question": question},
            "criteria": criteria,
        }
        for i, doc in enumerate(docs)
        for column, (question, criteria) in QUESTIONS.items()
    }
    return {"state": {"query": query, "searcher": "a web searcher in the United Kingdom"}, "questions": questions}


def score_chunk(query: str, docs: list[dict], api_key: str) -> list[list[float]]:
    body = request(query, docs)
    cache_path = CACHE / (sha1(json.dumps(body, sort_keys=True).encode()).hexdigest() + ".json")
    if cache_path.exists():
        response = json.loads(cache_path.read_text())
    else:
        response = call(body, api_key)
        cache_path.write_text(json.dumps(response))
    answers = response["answers"]
    return [[answers[f"{column[0]}{i}"]["score"] for column in COLUMNS] for i in range(len(docs))]


def engb_pools() -> dict[str, list[dict]]:
    """Per en-gb query, Staan's ten then stage 1's top 30, as `jev_ltr_eval.arms` pools them."""
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    text = json.loads((ENGB / "pool_text.json").read_text())
    pools = {}
    for query, entry in json.loads(RANKED.read_text()).items():
        staan = rows[query]["lists"]["staan"][:NUM_RESULTS]
        pages = {**text[query], **entry["pages"]}
        urls = dict.fromkeys(staan + entry["lists"][f"{STAGE1}, no MMR"])
        pools[query] = [{"url": url, "title": pages[url][0] or "", "extract": pages[url][1] or ""} for url in urls]
    return pools


def score():
    api_key = os.environ["JEV_API_KEY"]
    CACHE.mkdir(parents=True, exist_ok=True)
    *_, serving = load()
    pools = {
        query: group[["url", "title", "extract"]].fillna("").to_dict("records")
        for query, group in serving.drop_duplicates(["query", "url"]).groupby("query", sort=True)
    }
    pools.update(engb_pools())
    work = [
        (query, docs[i : i + CANDIDATES_PER_REQUEST])
        for query, docs in sorted(pools.items())
        for i in range(0, len(docs), CANDIDATES_PER_REQUEST)
    ]
    start = time.monotonic()
    with ThreadPoolExecutor(8) as pool:
        answers = list(pool.map(lambda chunk: score_chunk(*chunk, api_key), work))
    scores = {
        key(query, doc["url"]): values
        for (query, docs), chunk_scores in zip(work, answers)
        for doc, values in zip(docs, chunk_scores)
    }
    SCORES.write_text(json.dumps(scores))
    print(f"{len(scores)} pairs, {len(work)} requests in {time.monotonic() - start:.0f}s -> {SCORES}")


def composite(scores: np.ndarray, staan_position: np.ndarray, weights: tuple[float, float, float, float]):
    entity_weight, quality_weight, staan_weight, index_weight = weights
    from_index = staan_position == NOT_IN_STAAN
    return (
        scores[:, 0]
        + entity_weight * scores[:, 1]
        + quality_weight * scores[:, 2]
        - staan_weight * staan_position
        - index_weight * from_index
    )


def tune():
    scores = json.loads(SCORES.read_text())
    *_, serving = load()
    serving = serving.drop_duplicates(["query", "url"]).reset_index(drop=True)
    jev = np.array([scores[key(q, u)] for q, u in zip(serving["query"], serving["url"])])
    position = serving["staan_rank"].astype(float).fillna(NOT_IN_STAAN).to_numpy()
    keep = kept(serving, features(serving))
    gains = serving["overall"].to_numpy().astype(float)
    groups = list(serving.groupby("query").indices.values())

    def ndcg(weights) -> np.ndarray:
        ranking = composite(jev, position, weights)
        return np.array([ndcg_at_10(gains[g], ranking[g], keep[g]) for g in groups])

    baseline = ndcg((0.0, 0.0, TUNED_STAAN_WEIGHT, 0.0))
    results = []
    for weights in itertools.product(ENTITY_GRID, QUALITY_GRID, STAAN_GRID, INDEX_GRID):
        results.append((np.nanmean(ndcg(weights)), weights))
    results.sort(reverse=True)
    print(f"relevance only, w={TUNED_STAAN_WEIGHT}: {np.nanmean(baseline):.4f}")
    print("best (entity, quality, staan, index):")
    for value, weights in results[:10]:
        print(f"  {weights}: {value:.4f}")
    best = ndcg(results[0][1])
    diff = best - baseline
    rng = np.random.default_rng(0)
    means = [np.nanmean(diff[rng.integers(0, len(diff), len(diff))]) for _ in range(2000)]
    print(
        f"best vs relevance only: {np.nanmean(diff):+.4f} [{np.percentile(means, 2.5):+.4f}, {np.percentile(means, 97.5):+.4f}]"
    )
    for name, ablated in [
        ("no entity", (0.0, *results[0][1][1:])),
        ("no quality", (results[0][1][0], 0.0, *results[0][1][2:])),
        ("no index penalty", (*results[0][1][:3], 0.0)),
    ]:
        print(f"  {name}: {np.nanmean(ndcg(ablated)):.4f}")


def site_cap(urls: list[str], limit: int) -> list[str]:
    """`urls` with each host's results beyond its first `limit` moved, in order, to the end."""
    seen: Counter[str] = Counter()
    head, tail = [], []
    for url in urls:
        host = urlparse(url).netloc.removeprefix("www.")
        seen[host] += 1
        (head if seen[host] <= limit else tail).append(url)
    return head + tail


def arms(weights: tuple[float, float, float, float]):
    scores = json.loads(SCORES.read_text())
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    previous = json.loads(JEV_LTR_ARMS.read_text())
    out = {}
    for query, docs in engb_pools().items():
        staan = rows[query]["lists"]["staan"][:NUM_RESULTS]
        urls = [doc["url"] for doc in docs]
        jev = np.array([scores[key(query, url)] for url in urls])
        position = np.array([staan.index(url) if url in staan else NOT_IN_STAAN for url in urls], dtype=float)
        ranking = composite(jev, position, weights)
        ordered = [urls[i] for i in np.argsort(-ranking, kind="stable")]
        lists = {
            TUNED_BLEND: previous[query]["lists"][TUNED_BLEND],
            COMPOSITE: ordered[:NUM_RESULTS],
            COMPOSITE_CAP: site_cap(ordered, MAX_PER_SITE)[:NUM_RESULTS],
        }
        pages = {doc["url"]: [doc["title"], doc["extract"]] for doc in docs}
        pages.update(previous[query]["pages"])
        shown = {url for urls in lists.values() for url in urls}
        out[query] = {"lists": lists, "pages": {url: pages[url] for url in shown}}
    ARMS.write_text(json.dumps(out))
    differ = {
        name: sum(entry["lists"][name] != entry["lists"][TUNED_BLEND] for entry in out.values())
        for name in (COMPOSITE, COMPOSITE_CAP)
    }
    capped = sum(entry["lists"][COMPOSITE] != entry["lists"][COMPOSITE_CAP] for entry in out.values())
    print(f"{len(out)} queries -> {ARMS}; differ from {TUNED_BLEND}: {differ}; the cap changes {capped}")


def flags():
    """Mean of each question's score over the w=0.15 results the Brave comparison flagged, and the rest."""
    scores = json.loads(SCORES.read_text())
    old = {}
    for line in open(LABELS / "jev_engb_scores.jsonl"):
        record = json.loads(line)
        old[key(record["query"], record["url"])] = record["score"]
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    previous = json.loads(JEV_LTR_ARMS.read_text())
    flagged: dict[tuple[str, int], list[str]] = {}
    for line in open(BRAVE_JUDGMENTS):
        record = json.loads(line)
        side = "a_bad" if record["a"] == TUNED_BLEND else "b_bad"
        for position, reason in record["verdict"][side]:
            flagged.setdefault((record["query"], int(position)), []).append(reason)
    table = []
    for query, entry in previous.items():
        staan = rows[query]["lists"]["staan"][:NUM_RESULTS]
        for position, url in enumerate(entry["lists"][TUNED_BLEND], 1):
            if key(query, url) not in scores:
                continue
            reasons = flagged.get((query, position), [])
            table.append(
                {
                    "source": "staan" if url in staan else "index",
                    "flag": reasons[0] if reasons else "clean",
                    "old relevance": old.get(key(query, url), np.nan),
                    **dict(zip(COLUMNS, scores[key(query, url)])),
                }
            )
    frame = pd.DataFrame(table)
    frame["flag"] = frame["flag"].where(frame["flag"].isin(["clean", "off-topic", "thin", "duplicate"]), "other")
    print(frame.groupby(["source", "flag"]).agg(["mean", "count"]).round(2).to_string())


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "arms":
        arms(tuple(float(w) for w in sys.argv[2:6]))
    else:
        {"score": score, "tune": tune, "flags": flags}[command]()
