"""The Pass-2 candidate pool merge rule, shared by the collectors.

Lives on its own so the rule cannot drift between the original three-pool collector and any
later pass that adds a source to an already-collected pool: two copies of "keep the richest
title, first score wins, accumulate provenance" would eventually stop agreeing, and the
disagreement would show up as a feature shift in the training data rather than as an error.

Kept free of django.setup() and of any ranker, so importing it costs nothing.
"""


def add(
    pool: dict,
    url: str,
    title: str,
    extract: str,
    state,
    score,
    *,
    pool_tag: str,
    ss_source: str | None = None,
    gold_rank: int | None = None,
):
    """Merge one result into the per-url pool, keeping the richest title/extract.

    First writer wins for `score`, `state`, `ss_source` and `gold_rank`; `pools` accumulates.
    That the first score wins matters: a URL two sources both returned keeps the score of
    whichever was pooled first, which is how every row in the existing dataset was built.
    """
    if not url:
        return
    item = pool.setdefault(
        url,
        {
            "url": url,
            "title": "",
            "extract": "",
            "state": None,
            "score": None,
            "pools": [],
            "ss_source": None,
            "gold_rank": None,
        },
    )
    if title and len(title) > len(item["title"]):
        item["title"] = title
    if extract and len(extract) > len(item["extract"]):
        item["extract"] = extract
    if score is not None and item["score"] is None:
        item["score"] = float(score)
    if state is not None and item["state"] is None:
        item["state"] = state
    if pool_tag not in item["pools"]:
        item["pools"].append(pool_tag)
    if ss_source and not item["ss_source"]:
        item["ss_source"] = ss_source
    if gold_rank is not None and item["gold_rank"] is None:
        item["gold_rank"] = int(gold_rank)
