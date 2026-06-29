"""
LLM relabel — Pass 3 JUDGE: graded relevance + ethos + overall per (query, url).

Reads the Pass-2 pool and produces, for every pooled candidate, three blind
scores from a Haiku judge:
- relevance 0-3  : does THIS page answer THIS query (given the Pass-1 intent)?
- ethos      0-3 : intrinsic alignment with Mwmbl's values (open over closed,
                   truth over disinformation, independence over commercialism,
                   justice) — query-independent.
- overall    0-10: the single LTR ranking target; relevance dominates, ethos
                   breaks ties among comparably-relevant pages.

Candidates are presented BLIND — no provenance (standard / supersearch / google)
reaches the judge — so the calibration check (Google vs mwmbl vs SS grade
distributions) is unbiased.

The judging is driven by Haiku subagents in batches. This script prepares the
prompt + an id->(query,url) manifest (``--dump-batch``), merges a judge's output
back (``--merge``), reports progress (``--status``), and shows the per-pool grade
distributions (``--calibration``). Checkpoint is append-only JSONL keyed by
(query, url), so it is resumable and can stop early with a usable subset.

Usage::
    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" \
        uv run python scripts/llm_relabel_pass3_judge.py --dump-batch 5 --tag cal1 > batch.txt
    ... --merge judge_out.txt --tag cal1
    ... --calibration
"""
import json
import os
from argparse import ArgumentParser
from collections import Counter, defaultdict

PASS1 = "devdata/llm_relabel/pass1_intents.jsonl"
PASS2 = "devdata/llm_relabel/pass2_pool.jsonl"
CHECKPOINT = "devdata/llm_relabel/pass3_judgments.jsonl"
MANIFEST = "devdata/llm_relabel/pass3_manifest_{tag}.jsonl"
EXTRACT_CHARS = 400

JUDGE_PROMPT = """\
You are a careful search-quality judge for Mwmbl, an independent non-profit
search engine. You will see one or more queries; under each, its intent and a
numbered list of candidate web pages (title, URL, snippet). Score EACH candidate
on three axes.

You cannot tell which search engine produced any result, and you must not infer
it. Judge each page on its own merits. Do not reward a page for coming from a
famous or high-traffic domain.

────────────────────────────────────────────────────────
AXIS 1 — RELEVANCE (0-3): does THIS page answer THIS query, given the intent?
  3  Fully satisfies the intent.
       navigational -> the official destination the user wants
       informational -> a thorough, direct answer
       entity        -> an authoritative page about EXACTLY that entity
       transactional -> a page where you can actually do/buy/book the thing
       local         -> the right place/business/map result
       news          -> a current, on-topic report
  2  Relevant and useful, but partial or secondary.
  1  Marginal — on-topic-ish but thin, tangential, the wrong sense of an
     ambiguous term, or an SEO doorway that mentions the query without
     addressing it.
  0  Off-topic, wrong entity, spam, or broken.

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
     AI-spam; OR disinformation, propaganda, hateful or oppression-promoting content.

AXIS 3 — OVERALL (0-10): the single best-to-worst ranking score. Synthesise the
two axes — do NOT just add them:
  • Relevance is primary. A page that doesn't answer the query (relevance 0)
    cannot score above 2 overall, however high its ethos. A fully relevant page
    (relevance 3) starts in the upper range.
  • Among comparably-relevant pages, higher ethos ranks higher — this is the
    tie-breaker that produces the final order.
  • Honour the intent. For navigational/transactional queries the user's intended
    destination is the best result even when it is commercial: its relevance is 3,
    so it scores high — do not punish it for low ethos. For informational/open
    queries, let ethos separate the field more aggressively.
  • Use the full range. Reserve 9-10 for the single best result for a query; use
    0-1 for spam or wholly off-topic pages.

────────────────────────────────────────────────────────
WORKED EXAMPLES
(query: "blinds", intent: transactional)
  A retailer's window-blinds shop page           -> relevance 3, ethos 1, overall 7
  Wikipedia "Window blind" (CC-licensed)         -> relevance 2, ethos 3, overall 6
  Indie blog "$30 homebrew automated blinds"     -> relevance 1, ethos 3, overall 3
  Poker article "chopping the blinds"            -> relevance 0, ethos 2, overall 1
(query: "facebook login", intent: navigational)
  facebook.com login page                        -> relevance 3, ethos 1, overall 9
  A "how to log in to Facebook" SEO listicle     -> relevance 1, ethos 0, overall 1
(query: "vitamin d", intent: informational)
  NHS / peer-reviewed health page                -> relevance 3, ethos 3, overall 9
  Supplement shop "miracle cure" sales page      -> relevance 1, ethos 0, overall 2
(query: "gaza", intent: news)
  Human-rights org / primary first-hand report   -> relevance 3, ethos 3, overall 9
  Site justifying or whitewashing civilian harm  -> relevance 2, ethos 0, overall 2

────────────────────────────────────────────────────────
OUTPUT — one line per candidate, using its id, nothing else:
  <id> || <relevance 0-3> || <ethos 0-3> || <overall 0-10>

CANDIDATES TO JUDGE:
"""


def _load_jsonl(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def _pool():
    return {r["query"]: r for r in _load_jsonl(PASS2)}


def _intents():
    return {r["query"]: r["intent"] for r in _load_jsonl(PASS1)}


def _done_pairs():
    return {(r["query"], r["url"]) for r in _load_jsonl(CHECKPOINT)}


def cmd_dump_batch(n_queries: int, tag: str):
    pool, intents, done = _pool(), _intents(), _done_pairs()
    todo = [q for q, r in pool.items()
            if any((q, c["url"]) not in done for c in r["candidates"])]
    batch = todo[:n_queries]

    manifest, lines, cid = [], [], 0
    for q in batch:
        intent = intents.get(q, "?")
        lines.append(f"\n=== QUERY: {q}\n=== INTENT: {intent}")
        for c in pool[q]["candidates"]:
            if (q, c["url"]) in done:
                continue
            cid += 1
            manifest.append({"id": cid, "query": q, "url": c["url"]})
            title = c["title"] or "(no title)"
            extract = (c["extract"] or "").replace("\n", " ")[:EXTRACT_CHARS]
            lines.append(f"{cid}. {title} — {c['url']}\n   {extract}")

    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with open(MANIFEST.format(tag=tag), "w") as f:
        for m in manifest:
            f.write(json.dumps(m) + "\n")

    print(JUDGE_PROMPT + "\n".join(lines))


def cmd_merge(path: str, tag: str):
    by_id = {m["id"]: m for m in _load_jsonl(MANIFEST.format(tag=tag))}
    done = _done_pairs()
    added = skipped = bad = 0
    with open(CHECKPOINT, "a") as out:
        for line in open(path):
            line = line.strip()
            if not line or "||" not in line:
                continue
            parts = [p.strip() for p in line.split("||")]
            if len(parts) != 4:
                bad += 1
                continue
            try:
                cid, rel, eth, ov = (int(parts[0]), int(parts[1]),
                                     int(parts[2]), int(parts[3]))
            except ValueError:
                bad += 1
                continue
            m = by_id.get(cid)
            if not m or not (0 <= rel <= 3 and 0 <= eth <= 3 and 0 <= ov <= 10):
                bad += 1
                continue
            key = (m["query"], m["url"])
            if key in done:
                skipped += 1
                continue
            out.write(json.dumps({"query": m["query"], "url": m["url"],
                                  "relevance": rel, "ethos": eth, "overall": ov}) + "\n")
            done.add(key)
            added += 1
    print(f"merged {path} (tag={tag}): +{added} added, {skipped} dup, {bad} malformed")


def cmd_status():
    pool = _pool()
    judged = _load_jsonl(CHECKPOINT)
    total_cand = sum(len(r["candidates"]) for r in pool.values())
    qs_done = {j["query"] for j in judged}
    print(f"judged {len(judged)} / {total_cand} candidates; "
          f"queries touched {len(qs_done)} / {len(pool)}")
    if judged:
        for axis in ("relevance", "ethos", "overall"):
            dist = Counter(j[axis] for j in judged)
            print(f"  {axis:9s}: " + " ".join(f"{k}:{dist[k]}" for k in sorted(dist)))


def cmd_calibration():
    """Per-pool grade distributions — the blind Google vs mwmbl vs SS check."""
    pool = _pool()
    judged = {(j["query"], j["url"]): j for j in _load_jsonl(CHECKPOINT)}
    # url provenance from the pool
    buckets = defaultdict(list)   # pool tag -> list of judgments
    for q, r in pool.items():
        for c in r["candidates"]:
            j = judged.get((q, c["url"]))
            if not j:
                continue
            for tag in c["pools"]:
                buckets[tag].append(j)
    print(f"calibration over {len(judged)} judged candidates\n")
    print(f"{'pool':12s} {'n':>5} {'rel':>5} {'ethos':>6} {'overall':>8}   {'rel>=2 %':>8}")
    for tag in ("standard", "supersearch", "google"):
        js = buckets.get(tag, [])
        if not js:
            continue
        n = len(js)
        rel = sum(j["relevance"] for j in js) / n
        eth = sum(j["ethos"] for j in js) / n
        ov = sum(j["overall"] for j in js) / n
        good = 100 * sum(1 for j in js if j["relevance"] >= 2) / n
        print(f"{tag:12s} {n:5d} {rel:5.2f} {eth:6.2f} {ov:8.2f}   {good:7.1f}%")
    # SS broken down by source
    print("\nby super-search source:")
    src = defaultdict(list)
    for q, r in pool.items():
        for c in r["candidates"]:
            j = judged.get((q, c["url"]))
            if j and c["ss_source"]:
                src[c["ss_source"]].append(j)
    for s, js in sorted(src.items(), key=lambda kv: -len(kv[1])):
        n = len(js)
        rel = sum(j["relevance"] for j in js) / n
        print(f"  {s:18s} n={n:4d}  rel={rel:.2f}  overall={sum(j['overall'] for j in js)/n:.2f}")


def main():
    p = ArgumentParser()
    p.add_argument("--dump-batch", type=int, metavar="N_QUERIES")
    p.add_argument("--merge", metavar="FILE")
    p.add_argument("--tag", default="b1")
    p.add_argument("--status", action="store_true")
    p.add_argument("--calibration", action="store_true")
    a = p.parse_args()
    if a.merge:
        cmd_merge(a.merge, a.tag)
    elif a.dump_batch:
        cmd_dump_batch(a.dump_batch, a.tag)
    elif a.calibration:
        cmd_calibration()
    else:
        cmd_status()


if __name__ == "__main__":
    main()
