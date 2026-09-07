"""
LLM relabel — build the final learning-to-rank dataset.

Joins the Pass-2 candidate pool with the Pass-3 graded judgments into a drop-in
replacement for learning-to-rank.csv.gz. Same columns as the original
(gold_standard_rank, query, url, title, extract, state, score) plus the new
graded labels (relevance, ethos, overall) and provenance (pools, ss_source).

The intended LTR training target is ``overall`` (0-10); relevance and ethos are
kept for auditing and for re-deriving a combined target offline. Rows without a
Pass-3 judgment are dropped.

Usage::
    DATABASE_URL="postgres://daoud@" uv run python scripts/llm_relabel_build_dataset.py
"""

import json

import pandas as pd

from mwmbl.rankeval.paths import DATA_DIR

POOL = "devdata/llm_relabel/pass2_pool.jsonl"
JUDGE = "devdata/llm_relabel/pass3_judgments.jsonl"
OUT = str(DATA_DIR / "learning-to-rank-llm.csv.gz")


def main():
    judged = {}
    for line in open(JUDGE):
        line = line.strip()
        if line:
            j = json.loads(line)
            judged[(j["query"], j["url"])] = j

    rows = []
    for line in open(POOL):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        q = rec["query"]
        for c in rec["candidates"]:
            j = judged.get((q, c["url"]))
            if not j:
                continue
            rows.append(
                {
                    "gold_standard_rank": c["gold_rank"],
                    "query": q,
                    "url": c["url"],
                    "title": c["title"],
                    "extract": c["extract"],
                    "state": c["state"],
                    "score": c["score"],
                    "relevance": j["relevance"],
                    "ethos": j["ethos"],
                    "overall": j["overall"],
                    "pools": "|".join(c["pools"]),
                    "ss_source": c["ss_source"] or "",
                }
            )

    df = pd.DataFrame(rows)
    df.to_csv(OUT, errors="replace")
    print(f"Wrote {OUT}: {len(df)} rows, {df['query'].nunique()} queries")
    print("\nlabel means by pool membership:")
    for tag in ("standard", "supersearch", "google"):
        sub = df[df["pools"].str.contains(tag)]
        if len(sub):
            print(
                f"  {tag:12s} n={len(sub):6d}  relevance={sub.relevance.mean():.2f}  "
                f"ethos={sub.ethos.mean():.2f}  overall={sub.overall.mean():.2f}"
            )
    # how much graded signal lives outside the old binary (in-gold) label
    in_gold = df["gold_standard_rank"].notna()
    rel = df["relevance"] >= 2
    print(f"\nrelevant (relevance>=2): {rel.sum()}")
    print(f"  of which in Google gold : {(rel & in_gold).sum()}")
    print(
        f"  of which NOT in gold    : {(rel & ~in_gold).sum()} "
        f"({100 * (rel & ~in_gold).sum() / rel.sum():.0f}% recovered by the relabel)"
    )


if __name__ == "__main__":
    main()
