"""Score Combined Search orderings against Brave with the Haiku judgments, offline.

Every table in ``mwmbl/rankeval/combined-search-haiku-eval.md`` comes from this script. It
needs no network, no Django and no API keys: it reads the eval rows, candidate pools and
Haiku judgments committed under ``devdata/combined_providers_eval/``.

Three judgment sets, each blind (engine never shown) and shuffled:

- ``haiku/relevance_enus.jsonl``: relevance 0-3, no locale, over the pooled top tens of the
  ``en-us`` run (``rows-0.05.json``: Staan ``en-us``, Brave with no country, i.e. US).
- ``haiku/relevance_engb.jsonl``: relevance 0-3 for a UK searcher, over the ``en-gb`` run
  (``engb/rows-0.05.json``: Staan ``en-gb``, Brave ``country=GB``) plus every URL any
  re-ranking arm below puts in its top ten.
- ``haiku/pass3_engb.jsonl``: the same URLs judged with Mwmbl's own pass-3 prompt
  (``scripts/llm_relabel_pass3_judge.py``): relevance 0-3, ethos 0-3, overall 0-10.

The prompts are in ``haiku/prompts/``. An arm's NDCG@10 is scored against the ideal ordering
of every judged URL for the query, so adding a better candidate anywhere lowers everyone.

Usage::

    .venv/bin/python -m mwmbl.rankeval.evaluation.haiku_arms_report
"""

import json
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import numpy as np

EVAL_DIR = Path("devdata/combined_providers_eval")
NUM_RESULTS = 10
TRACKING_PARAMS = {"srsltid", "ref", "fbclid", "gclid"}
DISCOUNTS = 1 / np.log2(np.arange(2, NUM_RESULTS + 2))
BOOTSTRAP_SAMPLES = 4000


def load_json(path: str):
    return json.loads((EVAL_DIR / path).read_text())


def load_judgments(path: str) -> dict[str, dict[str, dict]]:
    judgments: dict[str, dict[str, dict]] = {}
    for line in (EVAL_DIR / path).read_text().splitlines():
        record = json.loads(line)
        judgments.setdefault(record["query"], {})[record["url"]] = record
    return judgments


def dcg(gains: list[float]) -> float:
    return float(np.sum(np.array(gains, dtype=float) * DISCOUNTS[: len(gains)]))


def ndcg(gains: list[float], all_gains: list[float]) -> float:
    ideal = dcg(sorted(all_gains, reverse=True)[:NUM_RESULTS])
    return dcg(gains) / ideal if ideal else 0.0


def bootstrap(differences: np.ndarray, rng: np.random.Generator) -> str:
    means = [differences[rng.integers(0, len(differences), len(differences))].mean() for _ in range(BOOTSTRAP_SAMPLES)]
    return f"{differences.mean():+.3f} [{np.percentile(means, 2.5):+.3f}, {np.percentile(means, 97.5):+.3f}]"


def print_table(title: str, scores: dict[str, list[float]], baselines: list[str]) -> None:
    rng = np.random.default_rng(0)
    print(f"\n## {title}\n")
    print("| Arm | Score | " + " | ".join(f"vs {b}, 95% CI" for b in baselines) + " |")
    print("|---|---|" + "---|" * len(baselines))
    for arm, values in scores.items():
        values = np.array(values)
        cells = [bootstrap(values - np.array(scores[b]), rng) for b in baselines]
        print(f"| {arm} | {values.mean():.3f} | " + " | ".join(cells) + " |")


def staan_first(staan: list[str], fill: list[str]) -> list[str]:
    """Staan's results in Staan's order, then the fill's other results, up to ten."""
    return (staan + [url for url in fill if url not in staan])[:NUM_RESULTS]


def normalise_url(url: str) -> str:
    """Strip tracking parameters, www., m. and trailing slashes, so gold can match."""
    parts = urlsplit(unquote(url))
    query = [(k, v) for k, v in parse_qsl(parts.query) if not (k.startswith("utm_") or k in TRACKING_PARAMS)]
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    return host + parts.path.rstrip("/") + ("?" + urlencode(query) if query else "")


def gold_ndcg(urls: list[str], gold: dict[str, float]) -> float:
    normalised_gold: dict[str, float] = {}
    for url, weight in gold.items():
        key = normalise_url(url)
        normalised_gold[key] = normalised_gold.get(key, 0.0) + weight
    seen, gains = set(), []
    for url in urls[:NUM_RESULTS]:
        key = normalise_url(url)
        gains.append(0.0 if key in seen else normalised_gold.get(key, 0.0))
        seen.add(key)
    # The ideal is the list's own gains re-sorted, as in compare_combined_providers.gold_ndcg
    # (sklearn's ndcg_score), so these line up with the numbers in the earlier handovers.
    return ndcg(gains, gains)


def report_enus() -> None:
    """Shipped vs Staan vs Brave, and the MiniLM re-sorts, on the en-us run."""
    rows = load_json("rows-0.05.json")
    judgments = load_judgments("haiku/relevance_enus.jsonl")
    scores: dict[str, list[float]] = {}
    gold: dict[str, list[float]] = {}
    for row in rows:
        graded = judgments.get(row["query"])
        if not graded:
            continue
        minilm, lists = row["judged"], row["lists"]
        combined, staan = lists["combined"][:NUM_RESULTS], lists["staan"][:NUM_RESULTS]
        union = list(dict.fromkeys(combined + staan))
        arms = {
            "combined (shipped)": combined,
            "staan": staan,
            "staan-first + fill": staan_first(staan, combined),
            "MiniLM re-sort of top 10": sorted(combined, key=lambda u: -minilm[u]),
            "MiniLM re-sort of combined ∪ Staan": sorted(union, key=lambda u: -minilm.get(u, 0))[:NUM_RESULTS],
            "brave": lists["brave"][:NUM_RESULTS],
        }
        all_gains = [2 ** g["relevance"] - 1 for g in graded.values()]
        for arm, urls in arms.items():
            gains = [2 ** graded[u]["relevance"] - 1 if u in graded else 0 for u in urls]
            scores.setdefault(arm, []).append(ndcg(gains, all_gains))
            gold.setdefault(arm, []).append(gold_ndcg(urls, row["gold"]))
    print(f"\n# en-us run ({len(scores['brave'])} queries)")
    print_table("Haiku relevance NDCG@10 (no locale)", scores, ["combined (shipped)", "brave"])
    print_table("Gold NDCG, URLs normalised", gold, ["combined (shipped)", "brave"])


def engb_arms(row: dict, deep: dict, wiki: dict) -> dict[str, list[str]]:
    """The en-gb arms: the baselines, and MiniLM over the LTR's top N with and without Wikipedia."""
    query, lists = row["query"], row["lists"]
    combined, staan = lists["combined"][:NUM_RESULTS], lists["staan"][:NUM_RESULTS]
    ltr, wikipedia = deep[query], wiki[query]
    minilm = {doc["url"]: doc["minilm"] for doc in ltr + wikipedia}

    def by_minilm(urls: list[str]) -> list[str]:
        return sorted(dict.fromkeys(urls), key=lambda u: -minilm.get(u, 0))[:NUM_RESULTS]

    wiki_urls = [doc["url"] for doc in wikipedia]
    arms = {"combined (shipped)": combined, "staan": staan, "staan-first + fill": staan_first(staan, combined)}
    for depth in (10, 20, 30):
        top = [doc["url"] for doc in ltr[:depth]]
        arms[f"MiniLM re-rank, LTR top {depth}"] = by_minilm(top)
        arms[f"MiniLM re-rank, LTR top {depth} + Wikipedia"] = by_minilm(top + wiki_urls)
    ltr_and_wiki = by_minilm([doc["url"] for doc in ltr] + wiki_urls)
    arms["staan-first, fill MiniLM(LTR top 30 + Wikipedia)"] = staan_first(staan, ltr_and_wiki)
    arms["brave"] = lists["brave"][:NUM_RESULTS]
    return arms


def report_engb() -> None:
    """Both engines asked for UK results; UK-relevance and pass-3 (relevance/ethos/overall) judges."""
    rows = load_json("engb/rows-0.05.json")
    deep, wiki = load_json("engb/combined_top30.json"), load_json("engb/wiki_pool.json")
    relevance = load_judgments("haiku/relevance_engb.jsonl")
    pass3 = load_judgments("haiku/pass3_engb.jsonl")

    uk_scores: dict[str, list[float]] = {}
    p3_scores: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        arms = engb_arms(row, deep, wiki)
        needed = {url for urls in arms.values() for url in urls}
        graded = relevance.get(row["query"], {})
        if needed <= set(graded):
            all_gains = [2 ** g["relevance"] - 1 for g in graded.values()]
            for arm, urls in arms.items():
                uk_scores.setdefault(arm, []).append(ndcg([2 ** graded[u]["relevance"] - 1 for u in urls], all_gains))
        judged = pass3.get(row["query"], {})
        if needed <= set(judged):
            for arm, urls in arms.items():
                ethos = [judged[u]["ethos"] for u in urls]
                metrics = p3_scores.setdefault(arm, {"overall": [], "relevance": [], "ethos": [], "low ethos": []})
                metrics["overall"].append(
                    ndcg([judged[u]["overall"] for u in urls], [j["overall"] for j in judged.values()])
                )
                metrics["relevance"].append(
                    ndcg(
                        [2 ** judged[u]["relevance"] - 1 for u in urls],
                        [2 ** j["relevance"] - 1 for j in judged.values()],
                    )
                )
                weights = DISCOUNTS[: len(ethos)]
                metrics["ethos"].append(float(np.sum(np.array(ethos) * weights) / np.sum(weights)) if ethos else 0.0)
                metrics["low ethos"].append(float(np.mean([e <= 1 for e in ethos])) if ethos else 0.0)

    print(f"\n# en-gb run, UK-relevance judge ({len(uk_scores['brave'])} queries)")
    print_table("Haiku UK relevance NDCG@10", uk_scores, ["staan-first + fill", "brave"])

    print(f"\n# en-gb run, pass-3 judge ({len(p3_scores['brave']['overall'])} queries)")
    rng = np.random.default_rng(0)
    base, brave = p3_scores["staan-first + fill"], p3_scores["brave"]
    print(
        "\n| Arm | Overall NDCG | Relevance NDCG | Ethos (position-weighted) | Top 10 with ethos ≤ 1 | Overall vs staan-first | Ethos vs brave |"
    )
    print("|---|---|---|---|---|---|---|")
    for arm, metrics in p3_scores.items():
        values = {name: np.array(v) for name, v in metrics.items()}
        overall_diff = bootstrap(values["overall"] - np.array(base["overall"]), rng)
        ethos_diff = bootstrap(values["ethos"] - np.array(brave["ethos"]), rng)
        print(
            f"| {arm} | {values['overall'].mean():.3f} | {values['relevance'].mean():.3f} | {values['ethos'].mean():.2f} | "
            f"{values['low ethos'].mean():.0%} | {overall_diff} | {ethos_diff} |"
        )

    print("\n## Pass-3 judgments by where the URL came from\n")
    print("| Source | URLs | Relevance | Ethos | Overall | Ethos ≤ 1 |")
    print("|---|---|---|---|---|---|")
    by_source: dict[str, list[tuple[int, int, int]]] = {}
    for row in rows:
        query, lists = row["query"], row["lists"]
        staan, brave_urls = set(lists["staan"][:NUM_RESULTS]), set(lists["brave"][:NUM_RESULTS])
        index = {doc["url"] for doc in deep[query][:30]} - staan
        wikipedia = {doc["url"] for doc in wiki[query]}
        for url, j in pass3.get(query, {}).items():
            if url in brave_urls and url not in staan:
                source = "Brave only"
            elif url in staan:
                source = "Staan"
            elif url in wikipedia:
                source = "Wikipedia"
            elif url in index:
                source = "Mwmbl index"
            else:
                continue
            by_source.setdefault(source, []).append((j["relevance"], j["ethos"], j["overall"]))
    for source, values in by_source.items():
        v = np.array(values)
        print(
            f"| {source} | {len(v)} | {v[:, 0].mean():.2f} | {v[:, 1].mean():.2f} | {v[:, 2].mean():.2f} | "
            f"{np.mean(v[:, 1] <= 1):.0%} |"
        )


if __name__ == "__main__":
    report_enus()
    report_engb()
