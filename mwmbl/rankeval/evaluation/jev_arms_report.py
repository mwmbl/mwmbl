"""Score Jev (TypeSafe) orderings of Combined Search's candidates with the Haiku judgments, offline.

Two ways of using Jev, each compared with the best MiniLM arm of
``mwmbl/rankeval/combined-search-haiku-eval.md`` on the en-gb run:

1. **Fill:** Staan's results in Staan's order, then the remaining slots filled from the LTR's
   top 30 plus Wikipedia, ordered by Jev.
2. **Re-rank:** Staan's top ten, the LTR's top 30 and Wikipedia pooled and ordered by Jev
   alone.

Jev's scores are read from ``haiku/jev_<name>.jsonl`` (one ``{"query", "url", "score"}`` per
line, higher is better), so several Jev question designs can be compared side by side. The
candidates they are scored over are exported by ``--export-candidates``.

A query counts only where Haiku has graded every URL any arm puts in its top ten; the number
left out is printed, and ``--dump-ungraded`` lists their URLs for another Haiku pass.

Usage::

    .venv/bin/python -m mwmbl.rankeval.evaluation.jev_arms_report --export-candidates
    .venv/bin/python -m mwmbl.rankeval.evaluation.jev_arms_report pointwise listwise
    .venv/bin/python -m mwmbl.rankeval.evaluation.jev_arms_report --minilm  # harness check
"""

import argparse
import json

from mwmbl.rankeval.evaluation.haiku_arms_report import (
    EVAL_DIR,
    NUM_RESULTS,
    load_json,
    load_judgments,
    ndcg,
    print_table,
    staan_first,
)

CANDIDATES_PATH = "engb/jev_candidates.json"
BASELINE = "staan-first, fill MiniLM(LTR top 30 + Wikipedia)"


def build_candidates() -> dict[str, list[dict]]:
    """Every candidate either arm can pick from, with the text a judge would see."""
    rows = load_json("engb/rows-0.05.json")
    pool_text = load_json("engb/pool_text.json")
    deep, wiki = load_json("engb/combined_top30.json"), load_json("engb/wiki_pool.json")
    candidates: dict[str, list[dict]] = {}
    for row in rows:
        query = row["query"]
        by_url: dict[str, dict] = {}
        for url in row["lists"]["staan"][:NUM_RESULTS]:
            title, extract, _ = pool_text[query][url]
            by_url[url] = {"url": url, "title": title, "extract": extract, "source": "staan"}
        for source, docs in (("index", deep[query]), ("wikipedia", wiki[query])):
            for doc in docs:
                by_url.setdefault(
                    doc["url"], {"url": doc["url"], "title": doc["title"], "extract": doc["extract"], "source": source}
                )
        candidates[query] = list(by_url.values())
    return candidates


def load_scores(name: str) -> dict[str, dict[str, float]]:
    scores: dict[str, dict[str, float]] = {}
    for line in (EVAL_DIR / f"haiku/jev_{name}.jsonl").read_text().splitlines():
        record = json.loads(line)
        scores.setdefault(record["query"], {})[record["url"]] = record["score"]
    return scores


def minilm_scores() -> dict[str, dict[str, float]]:
    """MiniLM's scores as if they were a Jev run, to check the harness reproduces the baseline.

    Staan results outside the LTR's top 30 were never scored by MiniLM; they get 0, so the
    MiniLM re-rank arm is a lower bound.
    """
    rows = load_json("engb/rows-0.05.json")
    deep, wiki = load_json("engb/combined_top30.json"), load_json("engb/wiki_pool.json")
    scores: dict[str, dict[str, float]] = {}
    for row in rows:
        query = row["query"]
        scores[query] = {url: 0.0 for url in row["lists"]["staan"][:NUM_RESULTS]}
        scores[query].update({doc["url"]: doc["minilm"] for doc in deep[query] + wiki[query]})
    return scores


def arms_for(row: dict, deep: dict, wiki: dict, runs: dict[str, dict[str, dict[str, float]]]) -> dict[str, list[str]]:
    query, lists = row["query"], row["lists"]
    staan = lists["staan"][:NUM_RESULTS]
    fill_pool = list(dict.fromkeys([doc["url"] for doc in deep[query]] + [doc["url"] for doc in wiki[query]]))
    minilm = {doc["url"]: doc["minilm"] for doc in deep[query] + wiki[query]}
    arms = {
        "combined (shipped)": lists["combined"][:NUM_RESULTS],
        "staan": staan,
        BASELINE: staan_first(staan, sorted(fill_pool, key=lambda u: -minilm[u])),
    }
    rerank_pool = list(dict.fromkeys(staan + fill_pool))
    for name, scores in runs.items():
        jev = scores[query]
        arms[f"staan-first, fill Jev {name}"] = staan_first(staan, sorted(fill_pool, key=lambda u: -jev[u]))
        arms[f"Jev {name} re-rank"] = sorted(rerank_pool, key=lambda u: -jev[u])[:NUM_RESULTS]
    arms["brave"] = lists["brave"][:NUM_RESULTS]
    return arms


def report(runs: dict[str, dict[str, dict[str, float]]], dump_ungraded: bool) -> None:
    rows = [row for row in load_json("engb/rows-0.05.json") if all(row["query"] in s for s in runs.values())]
    deep, wiki = load_json("engb/combined_top30.json"), load_json("engb/wiki_pool.json")
    relevance = load_judgments("haiku/relevance_engb.jsonl")
    pass3 = load_judgments("haiku/pass3_engb.jsonl")

    uk_scores: dict[str, list[float]] = {}
    overall_scores: dict[str, list[float]] = {}
    ungraded: dict[str, list[str]] = {}
    for row in rows:
        query = row["query"]
        arms = arms_for(row, deep, wiki, runs)
        needed = {url for urls in arms.values() for url in urls}
        graded, judged = relevance.get(query, {}), pass3.get(query, {})
        missing = sorted(needed - set(graded))
        if missing:
            ungraded[query] = missing
            continue
        all_gains = [2 ** g["relevance"] - 1 for g in graded.values()]
        for arm, urls in arms.items():
            uk_scores.setdefault(arm, []).append(ndcg([2 ** graded[u]["relevance"] - 1 for u in urls], all_gains))
        if needed <= set(judged):
            all_overall = [j["overall"] for j in judged.values()]
            for arm, urls in arms.items():
                overall_scores.setdefault(arm, []).append(ndcg([judged[u]["overall"] for u in urls], all_overall))

    num_missing = sum(len(urls) for urls in ungraded.values())
    print(f"\n{len(rows)} queries scored by every run; {len(ungraded)} left out for {num_missing} ungraded URLs.")
    if dump_ungraded:
        (EVAL_DIR / "engb/jev_ungraded.json").write_text(json.dumps(ungraded, indent=1))
        print("Ungraded URLs written to engb/jev_ungraded.json.")
    if uk_scores:
        print_table(f"Haiku UK relevance NDCG@10 ({len(uk_scores['brave'])} queries)", uk_scores, [BASELINE, "brave"])
    if overall_scores:
        print_table(
            f"Haiku pass-3 overall NDCG@10 ({len(overall_scores['brave'])} queries)",
            overall_scores,
            [BASELINE, "brave"],
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="*", help="names of haiku/jev_<name>.jsonl score files")
    parser.add_argument("--minilm", action="store_true", help="add MiniLM as a pseudo-run, to check the harness")
    parser.add_argument("--export-candidates", action="store_true")
    parser.add_argument("--dump-ungraded", action="store_true")
    args = parser.parse_args()

    if args.export_candidates:
        candidates = build_candidates()
        (EVAL_DIR / CANDIDATES_PATH).write_text(json.dumps(candidates, indent=1))
        total = sum(len(docs) for docs in candidates.values())
        print(f"{total} candidates over {len(candidates)} queries written to {CANDIDATES_PATH}.")
        return

    runs = {name: load_scores(name) for name in args.runs}
    if args.minilm:
        runs["minilm"] = minilm_scores()
    report(runs, args.dump_ungraded)


if __name__ == "__main__":
    main()
