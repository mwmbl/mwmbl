"""Holistic side-by-side evaluation of two rankings, judged by Claude Haiku.

Pass-3 NDCG grades each page alone, so it can't see a list's redundancy, which intents it
covers, how good its extracts are, or whether its #1 answers the query. Here a judge sees
two top-ten lists for one query and says which is better, by how much, and why. Each
comparison is judged twice, once in each order, in different batches, so different judges
see it. This is the second of the two evaluation steps in AGENTS.md; its validation and the
experiments it decided are in `mwmbl/rankeval/combined-holistic-eval.md`.

A lists file is JSON mapping each query to its ranked results, each a {"url", "title",
"extract"} object. Only the top ten count. X is the arm under test and Y the baseline, so
a positive preference favours X.

    batches X.json Y.json WORK   judge batches and a manifest -> WORK, for every query in
                                 both files whose top tens differ
    consolidate WORK             the judges' WORK/out_*.txt -> WORK/judgments.jsonl
    report WORK                  preference for X with a 95% CI, order consistency,
                                 position bias, reasons and flagged results

The judges were Claude Haiku 4.5 subagents, each given one batch file and asked to write its
JSON lines to the matching `out_*.txt` (batch_03.txt -> out_03.txt).

    uv run python -m mwmbl.rankeval.evaluation.holistic_judge batches x.json y.json work/
"""

import html
import json
import random
import re
from argparse import ArgumentParser
from collections import Counter
from pathlib import Path

import numpy as np

NUM_RESULTS = 10
EXTRACT_CHARS = 400
COMPARISONS_PER_BATCH = 25
BOOTSTRAP_SAMPLES = 2000
TAG = re.compile(r"<[^>]+>")
STRENGTHS = {"slight": 1, "clear": 2, "strong": 3}
REASONS = ("top_result", "relevance", "junk", "redundancy", "coverage", "extracts", "ethos", "none")
FLAGS = ("off-topic", "spam", "thin", "duplicate", "poor-extract", "wrong-locale", "ethos")

# Pass 3's ethos axis (scripts/llm_relabel_pass3_judge.py), word for word, so both
# evaluations hold results to the same values. A test keeps the two in step.
ETHOS_RUBRIC = """\
AXIS 2 — ETHOS (0-3): the intrinsic character of the page/source, INDEPENDENT
of this query. "Is this the kind of page a values-driven, independent search
engine wants to champion?" Score it even for off-topic pages. Mwmbl favours:
  • Open over closed — open-source, Creative Commons / open-access, freely
    readable and reusable content over proprietary, paywalled or login-walled.
  • Truth over disinformation — accurate, primary or well-evidenced sources over
    misleading, manipulative or conspiratorial content.
  • Independence over commercialism — primary/independent sources and substantive
    original work over SEO-spam, content farms, aggregators and ad/tracker bloat.
  • Justice — content that gives voice to the oppressed and upholds human rights
    over content that promotes oppression, hatred or dehumanisation (for example,
    it supports the Palestinian cause against occupation).
  Most everyday pages take no stance on truth or justice — judge those on the
  open/independence factors alone, neither rewarding nor penalising them on the
  axes that don't apply.

  3  Strongly aligned — open-licensed and/or a primary source, substantive and
     original, freely accessible and credible; OR content that clearly upholds
     human rights and gives voice to the marginalised.
  2  Legitimate and solid — an established publication, official org/institution
     page, or genuine project; accessible and credible, at most mild commercial
     framing, no values red flags.
  1  Weakly aligned — heavily commercial or SEO-tuned, aggregator/listicle/thin
     affiliate, or closed/paywalled, but not malicious.
  0  Against Mwmbl's values — content farm, doorway, scraper, ad-saturated page or
     AI-spam; OR disinformation, propaganda, hateful or oppression-promoting content."""

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
               [[3, "off-topic"], [7, "poor-extract"]]. Reasons: {", ".join(f'"{f}"' for f in FLAGS)}
  "b_bad":     the same for B
  "missed":    a few words on an intent neither list covers well, or ""
  "better":    "A", "B" or "tie"
  "strength":  "slight", "clear" or "strong" ("none" for a tie)
  "reason":    the main reason for the preference, one of {", ".join(f'"{r}"' for r in REASONS)}
  "note":      at most 20 words saying why

COMPARISONS:
"""


def results_block(label: str, results: list[dict]) -> str:
    lines = [f"--- RESULTS {label}"]
    for position, result in enumerate(results, 1):
        # Some providers mark query terms up with <strong>, which the searcher sees as bold.
        extract = html.unescape(TAG.sub("", result["extract"] or "")).replace("\n", " ")[:EXTRACT_CHARS]
        lines.append(f"{position}. {result['title'] or '(no title)'} — {result['url']}\n   {extract or '(no extract)'}")
    return "\n".join(lines)


def differing_queries(x_lists: dict, y_lists: dict) -> list[str]:
    def top_urls(results: list[dict]) -> list[str]:
        return [result["url"] for result in results[:NUM_RESULTS]]

    shared = sorted(set(x_lists) & set(y_lists))
    return [query for query in shared if top_urls(x_lists[query]) != top_urls(y_lists[query])]


def batches(x_path: Path, y_path: Path, work: Path) -> None:
    x_lists, y_lists = json.loads(x_path.read_text()), json.loads(y_path.read_text())
    lists = {"x": x_lists, "y": y_lists}
    queries = differing_queries(x_lists, y_lists)

    # The first orientation is random, and the two halves are shuffled and batched
    # separately, so no judge sees both orders of one comparison.
    rng = random.Random(1)
    halves: list[list[dict]] = [[], []]
    for comparison, query in enumerate(queries):
        x_first = rng.random() < 0.5
        for half in halves:
            a, b = ("x", "y") if x_first else ("y", "x")
            half.append({"comparison": comparison, "query": query, "a": a, "b": b})
            x_first = not x_first
    for half in halves:
        rng.shuffle(half)

    work.mkdir(parents=True, exist_ok=True)
    judgments = [judgment for half in halves for judgment in half]
    manifest = {"x": str(x_path), "y": str(y_path), "judgments": {}}
    num_batches = 0
    next_id = 1
    for half in halves:
        for start in range(0, len(half), COMPARISONS_PER_BATCH):
            blocks = []
            for judgment in half[start : start + COMPARISONS_PER_BATCH]:
                query = judgment["query"]
                a_results = lists[judgment["a"]][query][:NUM_RESULTS]
                b_results = lists[judgment["b"]][query][:NUM_RESULTS]
                header = f"\n=== COMPARISON {next_id} — QUERY: {query}"
                blocks.append("\n".join([header, results_block("A", a_results), results_block("B", b_results)]))
                manifest["judgments"][str(next_id)] = judgment
                next_id += 1
            (work / f"batch_{num_batches:02d}.txt").write_text(PROMPT + "\n".join(blocks) + "\n")
            num_batches += 1
    (work / "manifest.json").write_text(json.dumps(manifest))
    print(f"{len(queries)} comparisons, {len(judgments)} judgments in {num_batches} batches -> {work}")


def parse_verdicts(text: str) -> list[dict]:
    """The JSON lines of one judge's output, skipping any prose around them."""
    verdicts = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        # A judge occasionally repeats a key (a second "reason" holding the note): keep the first.
        verdict = json.loads(line, object_pairs_hook=lambda pairs: dict(reversed(pairs)))
        assert verdict["better"] in ("A", "B", "tie"), line
        assert verdict["better"] == "tie" or verdict["strength"] in STRENGTHS, line
        if verdict["reason"] not in REASONS:
            verdict["reason_given"], verdict["reason"] = verdict["reason"], "other"
        verdicts.append(verdict)
    return verdicts


def consolidate(work: Path) -> None:
    manifest = json.loads((work / "manifest.json").read_text())["judgments"]
    verdicts: dict[str, dict] = {}
    for path in sorted(work.glob("out_*.txt")):
        for verdict in parse_verdicts(path.read_text()):
            judgment_id = str(verdict["id"])
            assert judgment_id in manifest and judgment_id not in verdicts, (
                f"{path.name}: bad or repeated id {judgment_id}"
            )
            verdicts[judgment_id] = dict(verdict, judge=path.stem.removeprefix("out_"))
    missing = sorted(set(manifest) - set(verdicts), key=int)
    assert not missing, f"{len(missing)} ids unjudged, first {missing[:10]}"
    judgments_path = work / "judgments.jsonl"
    with open(judgments_path, "w") as f:
        for judgment_id, judgment in sorted(manifest.items(), key=lambda item: int(item[0])):
            record = dict(judgment, id=int(judgment_id), verdict=verdicts[judgment_id])
            f.write(json.dumps(record) + "\n")
    print(f"{len(verdicts)} judgments -> {judgments_path}")


def preference_for_x(record: dict) -> int:
    """The verdict as -3..3 in favour of X."""
    verdict = record["verdict"]
    if verdict["better"] == "tie":
        return 0
    size = STRENGTHS[verdict["strength"]]
    chosen = record["a"] if verdict["better"] == "A" else record["b"]
    return size if chosen == "x" else -size


def flag_counts(records: list[dict], arm: str) -> Counter:
    """How often judges flagged each kind of weak result in `arm`'s lists."""
    counts: Counter = Counter()
    for record in records:
        side = "a_bad" if record["a"] == arm else "b_bad"
        counts.update(flag[1] for flag in record["verdict"][side])
    return counts


def summarise(records: list[dict]) -> dict:
    by_comparison: dict[int, list[dict]] = {}
    for record in records:
        by_comparison.setdefault(record["comparison"], []).append(record)
    preferences = np.array([[preference_for_x(record) for record in pair] for pair in by_comparison.values()])
    combined = preferences.mean(axis=1)
    rng = np.random.default_rng(0)
    resampled = [combined[rng.integers(0, len(combined), len(combined))].mean() for _ in range(BOOTSTRAP_SAMPLES)]
    decided = [record["verdict"]["better"] for record in records if record["verdict"]["better"] != "tie"]
    signs = np.sign(preferences)
    return {
        "comparisons": len(combined),
        "judgments": len(records),
        "preference": float(combined.mean()),
        "ci": (float(np.percentile(resampled, 2.5)), float(np.percentile(resampled, 97.5))),
        "x_preferred": float(np.mean(combined > 0)),
        "y_preferred": float(np.mean(combined < 0)),
        "tie": float(np.mean(combined == 0)),
        "orders_agree": float(np.mean(signs[:, 0] == signs[:, 1])),
        "a_chosen": decided.count("A") / len(decided) if decided else float("nan"),
        "reasons": Counter(record["verdict"]["reason"] for record in records),
        "x_flags": flag_counts(records, "x"),
        "y_flags": flag_counts(records, "y"),
    }


def report(work: Path) -> None:
    manifest = json.loads((work / "manifest.json").read_text())
    records = [json.loads(line) for line in open(work / "judgments.jsonl")]
    summary = summarise(records)
    low, high = summary["ci"]
    print(f"X = {manifest['x']}, Y = {manifest['y']}\n")
    print(f"- {summary['comparisons']} comparisons, {summary['judgments']} judgments")
    print(f"- mean preference for X (-3..3): {summary['preference']:+.2f} [{low:+.2f}, {high:+.2f}]")
    print(f"- X preferred {summary['x_preferred']:.0%}, Y {summary['y_preferred']:.0%}, tie {summary['tie']:.0%}")
    print(f"- both orders agree on the direction: {summary['orders_agree']:.0%}")
    print(f"- A chosen in {summary['a_chosen']:.0%} of non-ties")
    print("- main reasons: " + ", ".join(f"{reason} {n}" for reason, n in summary["reasons"].most_common()))
    print(
        "- flagged results, X / Y: " + ", ".join(f"{f} {summary['x_flags'][f]}/{summary['y_flags'][f]}" for f in FLAGS)
    )


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    batches_parser = commands.add_parser("batches")
    batches_parser.add_argument("x", type=Path)
    batches_parser.add_argument("y", type=Path)
    batches_parser.add_argument("work", type=Path)
    for name in ("consolidate", "report"):
        commands.add_parser(name).add_argument("work", type=Path)
    args = parser.parse_args()

    if args.command == "batches":
        batches(args.x, args.y, args.work)
    elif args.command == "consolidate":
        consolidate(args.work)
    else:
        report(args.work)


if __name__ == "__main__":
    main()
