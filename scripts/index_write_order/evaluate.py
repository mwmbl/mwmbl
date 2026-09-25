"""Compare the heuristic and LTR write-time rankers on the en-gb eval queries.

    rank         the index-only ranking (Combined Search with nothing from Staan, as in
                 miss_probe.py) over each arm's index: the top ten, the candidate count, and
                 which of Brave's graded URLs are candidates.
    batches      blind, shuffled UK-relevance batches of every URL either arm puts in its
                 top ten, with the text that arm's index stored, for Claude Haiku 4.5 judges.
    pages        what each arm's pages for the lookup terms hold: documents per term, Hacker
                 News-listed share, repeated hosts. Needs the local indexes (build_index.py).
    consolidate  the judge output into haiku/relevance_engb_index_write_order.jsonl.
    report       recovery of Brave's good results, and Haiku NDCG@10 per arm. Reads only
                 committed data under devdata/combined_providers_eval/.

    PYTHONHASHSEED=0 DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" PYTHONPATH=. \
        uv run python scripts/index_write_order/evaluate.py rank|batches|consolidate|report

Ranker.retrieve walks a set of terms, so the order of equally scored results (two language
editions of one Wikipedia page, say) follows Python's string hashing: pin PYTHONHASHSEED or a
re-rank can swap a few of them. The committed results are the run that was judged.
"""

import json
import random
import sys
from pathlib import Path

import numpy as np

EVAL_DIR = Path("devdata/combined_providers_eval")
OUT_DIR = Path("devdata/index_write_order")
WORK_DIR = OUT_DIR / "judge"
RESULTS_DIR = EVAL_DIR / "index_write_order"
JUDGMENTS_PATH = EVAL_DIR / "haiku/relevance_engb_index_write_order.jsonl"
ARMS = ["heuristic", "ltr"]
NUM_RESULTS = 10
NUM_BATCHES = 16
GOOD_GRADE = 2


def rank():
    import django

    django.setup()
    from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
    from mwmbl.rankeval.evaluation.haiku_arms_report import normalise_url
    from mwmbl.search_setup import combined_ltr_model
    from mwmbl.tinysearchengine.indexer import Document, TinyIndex
    from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
    from mwmbl.tinysearchengine.mmr_rank import MMRRanker

    rows = json.loads((EVAL_DIR / "engb/rows-0.05.json").read_text())
    probe = json.loads((EVAL_DIR / "engb/miss_probe.json").read_text())
    brave_urls = {normalise_url(record["url"]) for record in probe}
    RESULTS_DIR.mkdir(exist_ok=True)
    for arm in ARMS:
        results = {}
        with TinyIndex(item_factory=Document, index_path=str(OUT_DIR / f"{arm}.tinysearch")) as index:
            inner = CombinedLTRRanker(index, DummyCompleter(), combined_ltr_model)
            ranker = MMRRanker(inner)
            for row in rows:
                query = row["query"]
                candidates = {normalise_url(doc.url) for doc in inner.retrieve(query).pages}
                top = ranker.search(query, [], False)[:NUM_RESULTS]
                results[query] = {
                    "top": [[doc.url, doc.title or "", doc.extract or ""] for doc in top],
                    "num_candidates": len(candidates),
                    "brave_candidates": sorted(candidates & brave_urls),
                }
        (RESULTS_DIR / f"{arm}.results.json").write_text(json.dumps(results, indent=1))
        print(arm, sum(len(r["top"]) for r in results.values()), "results")


def pages():
    import django

    django.setup()
    from build_corpus import LOOKUP

    from mwmbl.tinysearchengine.indexer import Document, TinyIndex
    from mwmbl.tinysearchengine.rank import get_domain_score
    from mwmbl.utils import get_domain

    print("| Arm | Documents per term, median | HN-listed share | Repeated hosts per term | Empty extracts |")
    print("|---|---|---|---|---|")
    for arm in ARMS:
        with TinyIndex(item_factory=Document, index_path=str(OUT_DIR / f"{arm}.tinysearch")) as index:
            term_documents = [index.retrieve(term) for term in sorted(LOOKUP)]
        documents = [document for docs in term_documents for document in docs]
        repeated = [len(docs) - len({get_domain(d.url) for d in docs}) for docs in term_documents]
        hn_share = np.mean([get_domain_score(d.url) > 0 for d in documents])
        empty = np.mean([not d.extract for d in documents])
        print(
            f"| {arm} | {np.median([len(docs) for docs in term_documents]):.0f} | {hn_share:.0%} | "
            f"{np.mean(repeated):.1f} | {empty:.0%} |"
        )


def load_results() -> dict[str, dict]:
    return {arm: json.loads((RESULTS_DIR / f"{arm}.results.json").read_text()) for arm in ARMS}


def batches():
    results = load_results()
    prompt = (EVAL_DIR / "haiku/prompts/relevance_engb.txt").read_text()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    manifest, lines = {}, [[] for _ in range(NUM_BATCHES)]
    for qid, query in enumerate(sorted(results["heuristic"])):
        # The same URL can carry different text in the two indexes; show one, the first seen.
        candidates = {}
        for arm in ARMS:
            for url, title, extract in results[arm][query]["top"]:
                candidates.setdefault(url, (title, extract))
        if not candidates:
            continue
        shown = sorted(candidates)
        random.Random(qid).shuffle(shown)
        manifest[qid] = [query, shown]
        block = [f"\n## qid {qid} | query: {query}\n"]
        for i, url in enumerate(shown):
            title, extract = candidates[url]
            block.append(f"[{i}] {title[:150]}\n    {url[:200]}\n    {extract.replace(chr(10), ' ')[:300]}\n")
        lines[qid % NUM_BATCHES] += block
    (WORK_DIR / "manifest.json").write_text(json.dumps(manifest))
    for b, block in enumerate(lines):
        (WORK_DIR / f"uk_{b}.txt").write_text(prompt + "\n=== QUERIES ===\n" + "".join(block))
    print(len(manifest), "queries,", sum(len(v[1]) for v in manifest.values()), "candidates in", WORK_DIR)


def consolidate():
    manifest = json.loads((WORK_DIR / "manifest.json").read_text())
    records = []
    for path in sorted(WORK_DIR.glob("uk_*.out.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip().startswith("{"):
                continue
            record = json.loads(line)
            query, shown = manifest[str(record["qid"])]
            # A judge that miscounts a query's candidates can't be lined up with them.
            if len(record["grades"]) != len(shown):
                print("skipping miscounted qid", record["qid"], query)
                continue
            records += [
                {"query": query, "url": shown[int(i)], "relevance": int(g)} for i, g in record["grades"].items()
            ]
    records.sort(key=lambda r: (r["query"], r["url"]))
    JUDGMENTS_PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    print(JUDGMENTS_PATH, len(records))


def load_grades() -> dict[str, dict[str, int]]:
    grades: dict[str, dict[str, int]] = {}
    for line in JUDGMENTS_PATH.read_text().splitlines():
        record = json.loads(line)
        grades.setdefault(record["query"], {})[record["url"]] = record["relevance"]
    return grades


def bootstrap(differences: np.ndarray) -> str:
    rng = np.random.default_rng(0)
    means = [differences[rng.integers(0, len(differences), len(differences))].mean() for _ in range(4000)]
    return f"{differences.mean():+.4f} [{np.percentile(means, 2.5):+.4f}, {np.percentile(means, 97.5):+.4f}]"


def report():
    from mwmbl.rankeval.evaluation.haiku_arms_report import dcg, ndcg, normalise_url

    results = load_results()
    probe = json.loads((EVAL_DIR / "engb/miss_probe.json").read_text())
    queries = sorted(results["heuristic"])

    # Brave's good results: the share of their gain each arm retrieves and ranks in its top ten.
    print("\n## Brave's results graded >= 2 (gain-weighted share, per-query mean)\n")
    print("| Set | Arm | In candidates | In top ten |")
    print("|---|---|---|---|")
    for name, keep in [
        ("all good", lambda r: r["grade"] >= GOOD_GRADE),
        ("targets (missed by prod)", lambda r: r["set"] == "target"),
        ("  of which evicted (2a)", lambda r: r["set"] == "target" and r["in_index"] and not r["candidate"]),
        ("controls (in prod top ten)", lambda r: r["set"] == "control"),
    ]:
        records = [r for r in probe if keep(r)]
        for arm in ARMS:
            gained = {"candidates": 0.0, "top": 0.0}
            total = 0.0
            for r in records:
                gain = (2 ** r["grade"] - 1) / np.log2(r["position"] + 1)
                key = normalise_url(r["url"])
                arm_result = results[arm][r["query"]]
                total += gain
                gained["candidates"] += gain * (key in set(arm_result["brave_candidates"]))
                gained["top"] += gain * (key in {normalise_url(u) for u, _, _ in arm_result["top"]})
            print(
                f"| {name} ({len(records)}) | {arm} | {gained['candidates'] / total:.1%} | {gained['top'] / total:.1%} |"
            )

    print("\n## Pool\n")
    for arm in ARMS:
        sizes = [results[arm][q]["num_candidates"] for q in queries]
        print(
            f"{arm}: median {np.median(sizes):.0f} candidates, {sum(len(results[arm][q]['top']) for q in queries)} results"
        )
    overlap = np.mean(
        [
            len({u for u, _, _ in results["heuristic"][q]["top"]} & {u for u, _, _ in results["ltr"][q]["top"]})
            / max(1, len(results["ltr"][q]["top"]))
            for q in queries
        ]
    )
    print(f"top-ten overlap between arms: {overlap:.0%}")

    grades = load_grades()
    brave_grades: dict[str, dict[str, int]] = {}
    for line in (EVAL_DIR / "haiku/relevance_engb.jsonl").read_text().splitlines():
        record = json.loads(line)
        brave_grades.setdefault(record["query"], {})[normalise_url(record["url"])] = record["relevance"]
    scores = {arm: [] for arm in ARMS}
    dcgs = {arm: [] for arm in ARMS}
    for query in queries:
        if query not in grades:
            continue
        judged = {normalise_url(url): g for url, g in grades[query].items()}
        # Ideal over everything judged for the query: both arms here, and Brave's pooled URLs.
        pool = {**brave_grades.get(query, {}), **judged}
        all_gains = [2**g - 1 for g in pool.values()]
        for arm in ARMS:
            gains = [2 ** grades[query][url] - 1 for url, _, _ in results[arm][query]["top"]]
            scores[arm].append(ndcg(gains, all_gains))
            dcgs[arm].append(dcg(gains))
    print(f"\n## Haiku UK relevance, {len(scores['ltr'])} queries\n")
    print("| Arm | NDCG@10 | Good results (>=2) per query |")
    print("|---|---|---|")
    for arm in ARMS:
        good = np.mean(
            [sum(grades[q][u] >= GOOD_GRADE for u, _, _ in results[arm][q]["top"]) for q in queries if q in grades]
        )
        print(f"| {arm} | {np.mean(scores[arm]):.4f} | {good:.2f} |")
    print(f"\nltr - heuristic NDCG@10: {bootstrap(np.array(scores['ltr']) - np.array(scores['heuristic']))}")


if __name__ == "__main__":
    {"rank": rank, "batches": batches, "pages": pages, "consolidate": consolidate, "report": report}[sys.argv[1]]()
