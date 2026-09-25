"""Where does Mwmbl's index lose Brave's good results? Offline tables from the committed data.

Every table in ``mwmbl/rankeval/combined-search-miss-attribution.md`` comes from this
script. It needs no network, no Django and no API keys.

A target is a Brave top-ten URL of the en-gb run (``engb/rows-0.05.json``) that Haiku graded
2 or 3 for a UK searcher (``haiku/relevance_engb.jsonl``) and that the index-only ranking
(Combined Search with nothing from Staan) does not put in its own top ten. Each target goes
to the first stage that fails, from the evidence ``scripts/combined_search_haiku/miss_probe.py``
recorded against the production index in ``engb/miss_probe.json``:

- blacklisted: the domain is on the blacklist, so it can never be served.
- 1 not in the index: no page the URL would be filed under (the first ten tokens and bigrams
  of Brave's title, snippet and the URL) holds it. Split by whether any other page of the
  same host turned up in those lookups.
- 2 in the index, not a candidate: the query's lookups don't return it. Split by whether it
  should be filed under one of the query's terms (so it was pushed off that term's page)
  or is filed only under other words.
- 3 a candidate, ranked out: zeroed by the majority-terms filter, scored <= 0 by the model,
  ranked below ten, or dropped by the title/URL de-duplication.

Separately, the text the index stored for every Brave URL it holds was re-graded with the
same prompt (``haiku/relevance_engb_indextext.jsonl``). Where it grades lower than Brave's
text of the same page, the index represents the page worse than Brave does: an extraction
problem. That is reported both for the targets and for the controls, the Brave URLs that
the index does rank in its top ten ("ranked fine but looks worse").

Each target is weighted by the gain it has in Brave's list, (2^g - 1) / log2(position + 1),
so a bucket's share of the weight is its share of what the misses cost in NDCG terms.

Usage::

    .venv/bin/python -m mwmbl.rankeval.evaluation.miss_attribution_report
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

EVAL_DIR = Path("devdata/combined_providers_eval")
NUM_RESULTS = 10
BOOTSTRAP_SAMPLES = 4000
BUCKETS = [
    "blacklisted",
    "1b not in index, host is",
    "1c not in index, host isn't either",
    "2a not a candidate, filed under a query term (evicted)",
    "2b not a candidate, filed under other words only",
    "3a candidate, majority-terms filter",
    "3b candidate, model score <= 0",
    "3c candidate, ranked below 10",
    "3d candidate, dropped as a duplicate",
]


def query_lookups(terms: list[str]) -> set[str]:
    return set(terms) | {" ".join(pair) for pair in zip(terms, terms[1:])}


def bucket(record: dict) -> str:
    if record["blacklisted"]:
        return BUCKETS[0]
    if not record["in_index"]:
        return BUCKETS[1] if record["host_in_index"] else BUCKETS[2]
    if not record["candidate"]:
        return BUCKETS[3] if record["probe_terms_overlapping_query"] else BUCKETS[4]
    if record["majority_filtered"]:
        return BUCKETS[5]
    if record["ltr_score"] <= 0:
        return BUCKETS[6]
    if record["index_rank"] is None:
        return BUCKETS[8]
    return BUCKETS[7]


def weight(record: dict) -> float:
    return (2 ** record["grade"] - 1) / np.log2(record["position"] + 1)


def host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].lower().removeprefix("www.")


def load_grades(path: str) -> dict[tuple[str, str], dict]:
    grades = {}
    for line in (EVAL_DIR / path).read_text().splitlines():
        record = json.loads(line)
        grades[(record["query"], record["url"])] = record
    return grades


def share_ci(targets: list[dict], name: str, rng: np.random.Generator) -> str:
    """A bucket's share of the target weight, with a bootstrap over queries."""
    by_query = defaultdict(lambda: [0.0, 0.0])
    for record in targets:
        by_query[record["query"]][1] += weight(record)
        if record["bucket"] == name:
            by_query[record["query"]][0] += weight(record)
    sums = np.array(list(by_query.values()))
    samples = [sums[idx].sum(0) for idx in rng.integers(0, len(sums), (BOOTSTRAP_SAMPLES, len(sums)))]
    shares = [s[0] / s[1] for s in samples]
    total = sums.sum(0)
    return f"{total[0] / total[1]:.1%} [{np.percentile(shares, 2.5):.1%}, {np.percentile(shares, 97.5):.1%}]"


def main():
    probe = json.loads((EVAL_DIR / "engb/miss_probe.json").read_text())
    for record in probe:
        record["bucket"] = bucket(record)
    targets = [r for r in probe if r["set"] == "target"]
    controls = [r for r in probe if r["set"] == "control"]
    rng = np.random.default_rng(0)

    print("# Miss attribution, en-gb run\n")
    print(
        f"{len({r['query'] for r in probe})} queries, {len(targets)} targets "
        f"({sum(r['grade'] == 3 for r in targets)} graded 3), {len(controls)} controls "
        f"(Brave URLs the index ranks in its top ten)."
    )

    print("\n## Buckets\n")
    print("| Bucket | Targets | Graded 3 | Share of lost gain, 95% CI |")
    print("|---|---|---|---|")
    counts = Counter(r["bucket"] for r in targets)
    counts3 = Counter(r["bucket"] for r in targets if r["grade"] == 3)
    for name in BUCKETS:
        print(f"| {name} | {counts[name]} | {counts3[name]} | {share_ci(targets, name, rng)} |")

    # How well the probe finds a page it has no query terms to help with: the controls are
    # known to be in the index, so any not found through their non-query terms alone is a
    # page the not-in-index test would have wrongly called missing.
    found = sum(bool(set(r["stored_terms"]) - query_lookups(r["query_terms"])) for r in controls)
    print(
        f"\nProbe check: {found} of {len(controls)} controls are found through terms other than "
        f"the query's own, so up to ~{1 - found / len(controls):.0%} of 'not in index' may be in it."
    )
    stored_counts = [len(set(r["stored_terms"])) for r in probe if r["stored_terms"]]
    print(
        f"A page found in the index is found under a median of {np.median(stored_counts):.0f} of the "
        f"~40-60 terms it is filed under; the rest have been pushed off their pages."
    )

    print("\n## By query length\n")
    print("| Query terms | Targets | " + " | ".join(n.split(" ")[0] for n in BUCKETS) + " |")
    print("|---|---|" + "---|" * len(BUCKETS))
    for label, test in (("1-2", lambda n: n <= 2), ("3-4", lambda n: 3 <= n <= 4), ("5+", lambda n: n >= 5)):
        group = [r for r in targets if test(len(r["query_terms"]))]
        group_counts = Counter(r["bucket"] for r in group)
        cells = [f"{group_counts[n] / len(group):.0%}" for n in BUCKETS]
        print(f"| {label} | {len(group)} | " + " | ".join(cells) + " |")

    print("\n## Hosts with the most targets not in the index\n")
    missing_hosts = Counter(host(r["url"]) for r in targets if not r["in_index"])
    missing_weight = defaultdict(float)
    for r in targets:
        if not r["in_index"]:
            missing_weight[host(r["url"])] += weight(r)
    print("| Host | Targets | Lost gain | Host otherwise in index |")
    print("|---|---|---|---|")
    host_seen = defaultdict(bool)
    for r in targets:
        host_seen[host(r["url"])] |= r["host_in_index"] or r["in_index"]
    for name, count in missing_hosts.most_common(25):
        print(f"| {name} | {count} | {missing_weight[name]:.1f} | {'yes' if host_seen[name] else 'no'} |")
    print(
        f"\n{len(missing_hosts)} hosts in all; the top 25 hold "
        f"{sum(c for _, c in missing_hosts.most_common(25)) / sum(missing_hosts.values()):.0%} of the targets not in the index."
    )

    indextext_path = EVAL_DIR / "haiku/relevance_engb_indextext.jsonl"
    if not indextext_path.exists():
        print("\n(No index-text judgments yet: run the Haiku step to get the extraction tables.)")
        return
    brave = load_grades("haiku/relevance_engb.jsonl")
    indextext = load_grades("haiku/relevance_engb_indextext.jsonl")

    anchors = [(k, g["relevance"]) for k, g in indextext.items() if g["anchor"]]
    drift = np.array([g - brave[k]["relevance"] for k, g in anchors])
    print(
        f"\n## Extraction: the index's text against Brave's, same page\n\n"
        f"Judge drift on {len(anchors)} anchors (same text, graded again): mean {drift.mean():+.2f}, "
        f"{np.mean(drift == 0):.0%} identical."
    )
    print("\n| Set | Pages | Index text lower | Same | Higher | Mean Brave text | Mean index text |")
    print("|---|---|---|---|---|---|---|")
    groups = {
        "controls (ranked in top ten)": controls,
        "targets in index": [r for r in targets if r["in_index"]],
        "  2 not a candidate": [r for r in targets if r["bucket"].startswith("2")],
        "  3 candidate, ranked out": [r for r in targets if r["bucket"].startswith("3")],
    }
    for label, group in groups.items():
        pairs = [
            (brave[(r["query"], r["url"])]["relevance"], indextext[(r["query"], r["url"])]["relevance"])
            for r in group
            if (r["query"], r["url"]) in indextext
        ]
        if not pairs:
            continue
        b, i = np.array(pairs).T
        print(
            f"| {label} | {len(pairs)} | {np.mean(i < b):.0%} | {np.mean(i == b):.0%} | {np.mean(i > b):.0%} "
            f"| {b.mean():.2f} | {i.mean():.2f} |"
        )

    print("\n## What the index stored for the pages it holds\n")
    print("| Stored text | Targets in index | of which graded lower | Controls | of which graded lower |")
    print("|---|---|---|---|---|")
    kinds = defaultdict(lambda: {"target": [0, 0], "control": [0, 0]})
    for r in targets + controls:
        key = (r["query"], r["url"])
        if r["stored"] is None or key not in indextext:
            continue
        cell = kinds[stored_text_kind(r["stored"])][r["set"]]
        cell[0] += 1
        cell[1] += indextext[key]["relevance"] < brave[key]["relevance"]
    for kind, cells in kinds.items():
        (t, tl), (c, cl) = cells["target"], cells["control"]
        print(f"| {kind} | {t} | {tl} | {c} | {cl} |")


ERROR_PAGE = re.compile(r"\b(403|404|ERROR|Access Denied|Just a moment|captcha|enable JavaScript|robot)", re.IGNORECASE)


def stored_text_kind(stored: dict) -> str:
    """A rough label for what the crawler stored: an error or bot-block page, no extract, or text."""
    if ERROR_PAGE.search(f"{stored['title'] or ''} {stored['extract'] or ''}"):
        return "error or bot-block page"
    if not (stored["extract"] or "").strip():
        return "empty extract"
    return "page text"


if __name__ == "__main__":
    main()
