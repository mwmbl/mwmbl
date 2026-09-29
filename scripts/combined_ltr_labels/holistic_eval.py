"""Holistic side-by-side evaluation of Combined Search result lists, judged by Haiku.

Pass-3 NDCG grades each page alone, so it can't see a list's redundancy, which intents it
covers, how good its extracts are, or whether its #1 answers the query. Here a judge sees
two top-ten lists for one query and says which is better, by how much, and why. Each
comparison is judged twice, once in each order, by different judges.

Before using it to compare arms, this validates it against what we already know:

    brave  Brave against `shipped` on every en-gb query where they differ. NDCG puts Brave
           0.09 ahead, so the judge should prefer Brave clearly.
    ndcg   For each query, the two of our arms in `engb_staan_arms.json` whose NDCG@10 differs
           most, where that is at least MIN_GAP. The judge should mostly agree on the sign.

The `mmr` experiment then compares the best learned arm with and without MMR, each against
REFERENCE, on every en-gb query:

    mmr     ndcg+new (en-gb Staan), with MMR, against REFERENCE.
    no-mmr  the same model without MMR, against REFERENCE, to separate MMR from the model.

Commands (experiment `validation` unless named):

    batches [experiment]      write the judge batches and a manifest -> its work directory
    consolidate [experiment]  judge output -> its judgments file
    report [experiment]       preference, agreement with NDCG, order consistency, reasons

Run from the repository root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and PYTHONPATH=.
"""

import html
import json
import random
import re
import sys
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

from scripts.combined_ltr_labels.engb_eval import ENGB, SHIPPED, judged
from scripts.llm_relabel_pass3_judge import EXTRACT_CHARS, JUDGE_PROMPT

LABELS = Path("devdata/combined_ltr_labels")
ARMS = LABELS / "engb_staan_arms.json"
REFERENCE = "staan-first, fill ndcg+new, no MMR"
MMR_ARMS = {"mmr": "ndcg+new (en-gb Staan)", "no-mmr": "ndcg+new (en-gb Staan), no MMR"}
BRAVE = "brave"
MIN_GAP = 0.05
MAX_NDCG_PAIRS = 150
COMPARISONS_PER_BATCH = 25
TAG = re.compile(r"<[^>]+>")
STRENGTHS = {"slight": 1, "clear": 2, "strong": 3}
REASONS = ("top_result", "relevance", "junk", "redundancy", "coverage", "extracts", "ethos", "none")

# Pass 3's ethos axis, word for word, so both evaluations hold results to the same values.
ETHOS_RUBRIC = JUDGE_PROMPT[JUDGE_PROMPT.index("AXIS 2") : JUDGE_PROMPT.index("AXIS 3")].strip()

PROMPT = f"""\
You are a careful search-quality judge for Mwmbl, an independent non-profit
search engine. The searcher is in the United Kingdom.

You will see a series of comparisons. Each has one query and two lists of search
results for it, RESULTS A and RESULTS B, each in ranked order with a title, URL
and extract (the snippet the searcher sees). Judge each pair of lists AS A WHOLE:
which page of results would serve this searcher better?

Infer the most likely intent of the query. Then consider, roughly in this order:
  1. The top result. Does #1 answer the query or reach the intended destination?
     The first few positions matter far more than the last few.
  2. Relevance. How many results are genuinely useful for the intent?
  3. Junk. Off-topic pages, spam, content farms, doorway pages, thin affiliate
     pages, and results for the wrong sense of an ambiguous query.
  4. Redundancy. Several results that say the same thing, or many pages from one
     site, add less than the same number of distinct useful results. But several
     pages from one site are fine when that site is what the searcher wants.
  5. Coverage. For an ambiguous or broad query, does the list cover the likely
     intents, or only one?
  6. Extracts. Does each extract let the searcher see what the page offers?
     Empty, garbled, boilerplate (cookie banners, navigation) or misleading
     extracts make results worse. Judge the result itself first: a strong result
     with a poor extract is still a strong result, but its poor extract is a flaw.
  7. Ethos, as Mwmbl's values below. It separates lists that are otherwise
     comparable, and counts for more on informational queries. Relevance comes
     first: on a navigational or transactional query the destination the searcher
     wants is the best result even when it is commercial. But harmful content
     (disinformation, hateful or oppression-promoting pages) anywhere in a list
     counts heavily against it, even low down.

{ETHOS_RUBRIC}

Judge only what you see. Don't reward a list for famous domains, and don't guess
which search engine produced it. If the lists are about equally good, say tie.

OUTPUT: one JSON object per comparison, one per line, nothing else, with keys in
this order:
  "id":        the comparison id
  "a_best":    position (1-10) of the best result in A, or 0 if none is useful
  "b_best":    the same for B
  "a_bad":     positions of weak results in A, each with a reason, e.g.
               [[3, "off-topic"], [7, "poor-extract"]]. Reasons: "off-topic",
               "spam", "thin", "duplicate", "poor-extract", "wrong-locale", "ethos"
  "b_bad":     the same for B
  "missed":    a few words on an intent neither list covers well, or ""
  "better":    "A", "B" or "tie"
  "strength":  "slight", "clear" or "strong" ("none" for a tie)
  "reason":    the main reason for the preference, one of {", ".join(f'"{r}"' for r in REASONS)}
  "note":      at most 20 words saying why

COMPARISONS:
"""


def ndcg_scores(arms: dict, rows: dict) -> dict[str, dict[str, float]]:
    """Pass-3 overall NDCG@10 per query and arm, as `engb_eval.report` computes it."""
    grades = judged()
    discounts = 1 / np.log2(np.arange(2, 12))
    scores: dict[str, dict[str, float]] = {}
    for query, entry in arms.items():
        lists = dict(entry["lists"], **{BRAVE: rows[query]["lists"][BRAVE][:10]})
        query_grades = grades.get(query, {})
        if not all(url in query_grades for urls in lists.values() for url in urls):
            continue
        ideal = np.sort([g["overall"] for g in query_grades.values()])[::-1][:10]
        ideal_dcg = float(np.sum(ideal * discounts[: len(ideal)]))
        scores[query] = {}
        for arm, urls in lists.items():
            gains = np.array([query_grades[url]["overall"] for url in urls], dtype=float)
            scores[query][arm] = float(np.sum(gains * discounts[: len(gains)])) / ideal_dcg
    return scores


def validation_comparisons(arms: dict, rows: dict, scores: dict) -> list[dict]:
    """The two validation sets, as (query, arm_x, arm_y) with arm_x's NDCG gain over arm_y."""
    found = []
    for query in sorted(scores):
        if arms[query]["lists"][SHIPPED] != rows[query]["lists"][BRAVE][:10]:
            gap = scores[query][BRAVE] - scores[query][SHIPPED]
            found.append({"set": "brave", "query": query, "x": BRAVE, "y": SHIPPED, "ndcg_gap": gap})

    rng = random.Random(0)
    widest = []
    for query in sorted(scores):
        ours = [arm for arm in arms[query]["lists"]]
        pairs = [(x, y) for i, x in enumerate(ours) for y in ours[i + 1 :]]
        pairs = [(x, y) for x, y in pairs if arms[query]["lists"][x] != arms[query]["lists"][y]]
        if not pairs:
            continue
        rng.shuffle(pairs)
        x, y = max(pairs, key=lambda pair: abs(scores[query][pair[0]] - scores[query][pair[1]]))
        gap = scores[query][x] - scores[query][y]
        if abs(gap) >= MIN_GAP:
            widest.append({"set": "ndcg", "query": query, "x": x, "y": y, "ndcg_gap": gap})
    rng.shuffle(widest)
    return found + widest[:MAX_NDCG_PAIRS]


def mmr_comparisons(arms: dict, rows: dict, scores: dict) -> list[dict]:
    """Each MMR_ARMS arm against REFERENCE, on every query where their lists differ."""
    return [
        {
            "set": name,
            "query": query,
            "x": arm,
            "y": REFERENCE,
            "ndcg_gap": scores[query][arm] - scores[query][REFERENCE],
        }
        for name, arm in MMR_ARMS.items()
        for query in sorted(scores)
        if arms[query]["lists"][arm] != arms[query]["lists"][REFERENCE]
    ]


EXPERIMENTS = {
    "validation": (validation_comparisons, LABELS / "holistic_work", LABELS / "holistic_judgments.jsonl"),
    "mmr": (mmr_comparisons, LABELS / "holistic_mmr_work", LABELS / "holistic_mmr_judgments.jsonl"),
}


def results_block(label: str, urls: list[str], pages: dict) -> str:
    lines = [f"--- RESULTS {label}"]
    for position, url in enumerate(urls, 1):
        title, extract = pages[url][:2]
        # Brave marks query terms up with <strong>, which the searcher sees as bold.
        extract = html.unescape(TAG.sub("", extract or "")).replace("\n", " ")[:EXTRACT_CHARS]
        lines.append(f"{position}. {title or '(no title)'} — {url}\n   {extract or '(no extract)'}")
    return "\n".join(lines)


def batches(experiment: str):
    find, work, _ = EXPERIMENTS[experiment]
    arms = json.loads(ARMS.read_text())
    rows = {row["query"]: row for row in json.loads((ENGB / "rows-0.05.json").read_text())}
    text = json.loads((ENGB / "pool_text.json").read_text())
    scores = ndcg_scores(arms, rows)
    found = find(arms, rows, scores)

    # Each comparison is judged in both orders, the second in a different batch: the first
    # orientation is random, and the two halves are shuffled and batched separately.
    rng = random.Random(1)
    halves: list[list[dict]] = [[], []]
    for c, comparison in enumerate(found):
        flipped = rng.random() < 0.5
        for half in (0, 1):
            x_first = flipped == bool(half)
            a, b = (comparison["x"], comparison["y"]) if x_first else (comparison["y"], comparison["x"])
            halves[half].append(dict(comparison, comparison=c, a=a, b=b))
    for half in halves:
        rng.shuffle(half)

    work.mkdir(parents=True, exist_ok=True)
    manifest, next_id, batch = {}, 1, 0
    for half in halves:
        for start in range(0, len(half), COMPARISONS_PER_BATCH):
            blocks = []
            for judgment in half[start : start + COMPARISONS_PER_BATCH]:
                query = judgment["query"]
                blocks.append(
                    "\n".join(
                        [
                            f"\n=== COMPARISON {next_id} — QUERY: {query}",
                            results_block(
                                "A",
                                list_for(judgment["a"], query, arms, rows),
                                pages_for(judgment["a"], query, arms, text),
                            ),
                            results_block(
                                "B",
                                list_for(judgment["b"], query, arms, rows),
                                pages_for(judgment["b"], query, arms, text),
                            ),
                        ]
                    )
                )
                manifest[next_id] = judgment
                next_id += 1
            (work / f"batch_{batch:02d}.txt").write_text(PROMPT + "\n".join(blocks) + "\n")
            batch += 1
    (work / "manifest.json").write_text(json.dumps(manifest))
    sets = {name: sum(1 for c in found if c["set"] == name) for name in dict.fromkeys(c["set"] for c in found)}
    print(f"{len(found)} comparisons {sets}, {len(manifest)} judgments in {batch} batches")


def list_for(arm: str, query: str, arms: dict, rows: dict) -> list[str]:
    return rows[query]["lists"][BRAVE][:10] if arm == BRAVE else arms[query]["lists"][arm]


def pages_for(arm: str, query: str, arms: dict, text: dict) -> dict:
    """The title and extract each list served: Brave's (or Staan's, where they share a URL)
    for Brave, and the ranker's own for our arms. The Staan-first arms were assembled after
    ranking, so their Staan results fall back to Staan's text."""
    return text[query] if arm == BRAVE else {**text[query], **arms[query]["pages"]}


def consolidate(experiment: str):
    _, work, judgments = EXPERIMENTS[experiment]
    manifest = {int(k): v for k, v in json.loads((work / "manifest.json").read_text()).items()}
    verdicts = {}
    for path in sorted(work.glob("out_*.txt")):
        for line in path.read_text().splitlines():
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            # A judge occasionally repeats a key (a second "reason" holding the note): keep the first.
            verdict = json.loads(line, object_pairs_hook=lambda pairs: dict(reversed(pairs)))
            cid = int(verdict["id"])
            assert cid in manifest and cid not in verdicts, f"{path.name}: bad or repeated id {cid}"
            assert verdict["better"] in ("A", "B", "tie"), f"{path.name}: {line}"
            assert verdict["better"] == "tie" or verdict["strength"] in STRENGTHS, f"{path.name}: {line}"
            if verdict["reason"] not in REASONS:
                verdict["reason_given"], verdict["reason"] = verdict["reason"], "other"
            verdicts[cid] = dict(verdict, judge=path.stem.removeprefix("out_"))
    missing = sorted(set(manifest) - set(verdicts))
    assert not missing, f"{len(missing)} ids unjudged, first {missing[:10]}"
    with open(judgments, "w") as f:
        for cid in sorted(manifest):
            f.write(json.dumps(dict(manifest[cid], id=cid, verdict=verdicts[cid])) + "\n")
    print(f"{len(verdicts)} judgments -> {judgments}")


def preference_for_x(record: dict) -> int:
    """The verdict as -3..3 in favour of the comparison's arm x."""
    verdict = record["verdict"]
    if verdict["better"] == "tie":
        return 0
    size = STRENGTHS[verdict["strength"]]
    chosen = record["a"] if verdict["better"] == "A" else record["b"]
    return size if chosen == record["x"] else -size


def report(experiment: str):
    _, _, judgments = EXPERIMENTS[experiment]
    records = [json.loads(line) for line in open(judgments)]
    by_comparison: dict[int, list[dict]] = {}
    for record in records:
        by_comparison.setdefault(record["comparison"], []).append(record)

    first_position = np.mean([r["verdict"]["better"] == "A" for r in records if r["verdict"]["better"] != "tie"])
    print(f"{len(by_comparison)} comparisons, {len(records)} judgments; A chosen in {first_position:.0%} of non-ties")

    rng = np.random.default_rng(0)
    for name in dict.fromkeys(record["set"] for record in records):
        pairs = [pair for pair in by_comparison.values() if pair[0]["set"] == name]
        prefs = np.array([[preference_for_x(r) for r in pair] for pair in pairs])
        gaps = np.array([pair[0]["ndcg_gap"] for pair in pairs])
        combined = prefs.mean(axis=1)
        signs = np.sign(prefs)
        consistent = np.mean(signs[:, 0] == signs[:, 1])
        means = [combined[rng.integers(0, len(combined), len(combined))].mean() for _ in range(2000)]
        decided = combined != 0
        agree = np.mean(np.sign(combined[decided]) == np.sign(gaps[decided]))
        rho = spearmanr(gaps, combined).statistic
        print(f"\n## {name}: {len(pairs)} comparisons (x = the arm NDCG measures against y)\n")
        print(
            f"- mean preference for x (-3..3): {combined.mean():+.2f} [{np.percentile(means, 2.5):+.2f}, {np.percentile(means, 97.5):+.2f}]"
        )
        print(
            f"- x preferred {np.mean(combined > 0):.0%}, y {np.mean(combined < 0):.0%}, tie {np.mean(combined == 0):.0%}"
        )
        print(f"- both orders agree on the direction: {consistent:.0%}")
        print(f"- judge agrees with NDCG's sign where it decides: {agree:.0%} of {decided.sum()}")
        print(f"- Spearman(NDCG gap, preference): {rho:.2f}")
        for low, high in ((0, 0.05), (0.05, 0.1), (0.1, 0.2), (0.2, 1.1)):
            band = (np.abs(gaps) >= low) & (np.abs(gaps) < high) & decided
            if band.any():
                print(
                    f"  - |gap| {low:.2f}-{high:.2f}: agrees {np.mean(np.sign(combined[band]) == np.sign(gaps[band])):.0%} of {band.sum()}"
                )
        reasons: dict[str, int] = {}
        for pair in pairs:
            for record in pair:
                reason = record["verdict"]["reason"]
                reasons[reason] = reasons.get(reason, 0) + 1
        print("- main reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])))
        disagreements = sorted(
            (pair for pair, c, g in zip(pairs, combined, gaps) if c * g < 0),
            key=lambda pair: -abs(pair[0]["ndcg_gap"]),
        )
        print("- widest disagreements:")
        for pair in disagreements[:8]:
            head = pair[0]
            notes = " / ".join(r["verdict"].get("note", "") for r in pair)
            print(f"  - {head['query']!r}: {head['x']} vs {head['y']}, NDCG {head['ndcg_gap']:+.2f}; {notes}")


if __name__ == "__main__":
    command = {"batches": batches, "consolidate": consolidate, "report": report}[sys.argv[1]]
    command(sys.argv[2] if len(sys.argv) > 2 else "validation")
