"""
PoC: collect standard-search vs Super Search results for a handful of train
queries, alongside the scraped-Google gold, into one Markdown file for manual
relevance review.

This script only *collects* and dumps; the relevance judgement is done by a human
(or Claude) reading the output file. The question it serves: when Super Search
results don't match the Google gold, is that because we return relevant pages from
different domains, or because we return genuinely irrelevant pages?

Usage::

    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
        uv run python scripts/super_search_manual_poc.py [--num 10] [--seed 42] [--out PATH]

Needs the local index + LTR model (via search_setup), network access for the first
Super Search run (joblib-cached afterwards), and Redis for crawl/robots caching.
"""
import asyncio
import os
from argparse import ArgumentParser
from urllib.parse import urlsplit

import django
import numpy as np
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

# Imported after django.setup(): these pull in search_setup (index + model).
from mwmbl.rankeval.evaluation.evaluate import NUM_RESULTS_FOR_EVAL  # noqa: E402
from mwmbl.rankeval.evaluation.evaluate_super_search import (  # noqa: E402
    _collect_docs,
    _dict_to_doc,
    _emit_final_results,
)
from mwmbl.rankeval.paths import RANKINGS_DATASET_TRAIN_PATH  # noqa: E402
from mwmbl.search_setup import ranker  # noqa: E402  (local index + LTR + MMR + wiki)


def domain(url: str) -> str:
    """Registrable-ish domain: netloc minus a leading 'www.'. Good enough for the PoC."""
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def collect_super_search(query: str) -> list[dict]:
    """Run the real SS final ranking and return full result items (not just URLs)."""
    docs = [_dict_to_doc(d) for d in _collect_docs(query)]

    captured: list[dict] = []

    async def emit(event_type, data):
        if event_type == "results":
            captured.clear()
            for item in data.results:
                captured.append({
                    "url": item.url, "title": item.title, "extract": item.extract,
                    "score": item.score, "source": item.source,
                })

    async def run():
        await _emit_final_results(query, docs, emit, [None], asyncio.Lock())

    asyncio.run(run())
    return captured


def fmt_panel(title: str, rows: list[dict], gold_domains: set[str]) -> list[str]:
    lines = [f"### {title}", ""]
    if not rows:
        lines += ["_(no results)_", ""]
        return lines
    matched = 0
    for i, r in enumerate(rows[:NUM_RESULTS_FOR_EVAL], 1):
        flag = ""
        if domain(r["url"]) in gold_domains:
            flag = " `[gold-domain]`"
            matched += 1
        src = f" _(source: {r['source']})_" if r.get("source") else ""
        score = f" score={r['score']:.4f}" if r.get("score") is not None else ""
        lines.append(f"{i}. **{r.get('title') or '(no title)'}**{flag}{src}{score}")
        lines.append(f"   <{r['url']}>")
        extract = (r.get("extract") or "").strip().replace("\n", " ")
        if extract:
            lines.append(f"   > {extract[:300]}")
        lines.append("")
    lines.insert(1, f"_{matched}/{min(len(rows), NUM_RESULTS_FOR_EVAL)} share a gold domain_\n")
    return lines


def main():
    parser = ArgumentParser()
    parser.add_argument("--num", type=int, default=10, help="Number of queries to sample.")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for sampling.")
    parser.add_argument("--out", default=os.path.join(
        os.environ.get("CLAUDE_SCRATCHPAD", "."), "ss_manual_poc.md"))
    args = parser.parse_args()

    dataset = pd.read_csv(RANKINGS_DATASET_TRAIN_PATH)
    queries = dataset["query"].unique()
    rng = np.random.default_rng(args.seed)
    sample = rng.choice(queries, args.num, replace=False)

    out_lines = [
        "# Super Search manual relevance PoC",
        f"\n{args.num} train queries (seed {args.seed}). For each: scraped-Google gold, "
        "standard search, Super Search. `[gold-domain]` flags a result whose domain "
        "appears in the gold set.\n",
    ]

    for n, query in enumerate(sample, 1):
        print(f"[{n}/{len(sample)}] {query!r}")
        gold = (dataset[dataset["query"] == query]
                .sort_values("rank")
                .head(NUM_RESULTS_FOR_EVAL))
        gold_rows = [{"url": r.url, "title": None, "extract": r.snippet,
                      "score": None, "source": ""} for r in gold.itertuples()]
        gold_domains = {domain(r["url"]) for r in gold_rows}

        std_docs = ranker.search(query, [])
        std_rows = [{"url": d.url, "title": d.title, "extract": d.extract,
                     "score": d.score, "source": ""} for d in std_docs]

        try:
            ss_rows = collect_super_search(query)
        except Exception as e:  # noqa: BLE001 — PoC: never let one query abort the run
            print(f"    super search failed: {e!r}")
            ss_rows = []

        out_lines.append(f"\n---\n\n## {n}. `{query}`\n")
        out_lines += fmt_panel("Gold (Google)", gold_rows, gold_domains)
        out_lines += fmt_panel("Standard search", std_rows, gold_domains)
        out_lines += fmt_panel("Super Search", ss_rows, gold_domains)

    with open(args.out, "w") as f:
        f.write("\n".join(out_lines))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
