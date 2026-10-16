#!/usr/bin/env python3
"""Transform the export_judgments dump into judge-training preference pairs.

Reads devdata/judgments_export/*.jsonl.gz and emits:

- ``pairs.jsonl.gz``     preference pairs {query, pos, neg, rule, table, user}
  for pairwise fine-tuning (margin/ranking loss).
- ``pointwise.jsonl.gz`` pointwise labels {query, url, title, extract, label}
  from votes (+1/-1) and old-interface validations (+1).
- ``transform_report.json`` counts by rule and filter, printed to stdout.

Noise filters (applied before any pair derivation):
- users with an ACCEPTED moderation flag on any curation are dropped entirely;
- "domain spammers" are dropped entirely: users with >= MIN_ADDS_FOR_SPAM add
  events of which a single domain accounts for > SPAM_DOMAIN_SHARE (this
  catches the 12bytes.org mass-injector in the old table and the twitter.com
  single-query flooder in the new one, without hardcoding user ids);
- curations whose query is itself a URL/domain are dropped (the self-promotion
  add signature: search your own URL, add it);
- no-op curations (identical URL order) contribute nothing.

Pair derivation:
- current-interface curations: an added result beats up to PAIR_CAP originals
  ranked below it; for results present in both lists, every order inversion
  (u above v before, v above u after) yields v > u, capped per winner;
- old-interface events: move-up beats the results jumped over; add beats the
  results below the insertion point; delete loses to kept results that ranked
  below it (session state tracked per user+query to resolve delete indices);
- votes / validates become pointwise labels.

Text backfill: many user-added results are bare URLs. Missing title/extract is
first filled from a free local lookup (every documented URL in the export plus
the Pass-2 candidate pool); ``--backfill`` then crawls the remainder over HTTP
(checkpointed to backfill_cache.jsonl.gz, resumable).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PAIR_CAP = 5  # max losers per positive event
MIN_ADDS_FOR_SPAM = 20  # dominance filter threshold
SPAM_DOMAIN_SHARE = 0.5
APPROVED_STATE = 7  # DocumentState values >= this are curated/approved
PASS2_POOL = Path("devdata/llm_relabel/pass2_pool.jsonl")


def load_jsonl(path: Path) -> list[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
        return [json.loads(line) for line in f]


def domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


URLISH_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?")

# No-NSFW policy: pairs touching adult content are excluded from training data.
# Word-boundary matching so e.g. Essex/Sussex don't trip the "sex" term.
NSFW_RE = re.compile(
    r"\b(porn\w*|xxx+|hentai|milfs?|nsfw|xvideos?|xhamster|redtube|youporn|"
    r"brazzers|onlyfans|camgirls?|camsex|livejasmin|chaturbate|stripchat|"
    r"erotic\w*|escorts?|sex|sexcam\w*|blowjob\w*|anal|cumshot\w*|gangbang\w*|"
    r"nudes?|naked|fetish\w*|bdsm|dildos?|masturbat\w*)\b",
    re.IGNORECASE,
)


def is_nsfw(*texts) -> bool:
    return any(NSFW_RE.search(t) for t in texts if t)


def is_urlish_query(query: str) -> bool:
    """A query that is itself a URL/domain — the self-promotion add signature
    (search your own URL, add it), and useless as judge-training input."""
    query = query.strip().lower()
    return query.startswith("http") or "://" in query or bool(URLISH_RE.fullmatch(query))


def query_from_results_url(url: str) -> str | None:
    try:
        params = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    values = params.get("q")
    return values[0] if values else None


def added_urls(curation: dict) -> list[str]:
    original = {d["url"] for d in curation["original_results"]}
    return [d["url"] for d in curation["new_results"] if d["url"] not in original]


def find_spammers(curations: list[dict], user_curations: list[dict]) -> dict[str, str]:
    """user -> dominant domain, for users whose adds are one-domain floods."""
    adds_by_user = defaultdict(Counter)
    for curation in curations:
        for url in added_urls(curation):
            adds_by_user[curation["user"]][domain(url)] += 1
    for event in user_curations:
        if event["curation_type"] == "curate_add":
            url = (event["curation"] or {}).get("url", "")
            if url:
                adds_by_user[event["user"]][domain(url)] += 1

    spammers = {}
    for user, domains in adds_by_user.items():
        if user is None:
            continue
        total = sum(domains.values())
        top_domain, top_count = domains.most_common(1)[0]
        if total >= MIN_ADDS_FOR_SPAM and top_count / total > SPAM_DOMAIN_SHARE:
            spammers[user] = top_domain
    return spammers


def flagged_users(curations: list[dict]) -> set[str]:
    return {c["user"] for c in curations if c["user"] and any(f["status"] == "ACCEPTED" for f in c["flags"])}


class TextLookup:
    """url -> (title, extract) from every documented URL we have locally."""

    def __init__(self):
        self.texts: dict[str, tuple[str, str]] = {}

    def learn(self, doc: dict):
        title = (doc.get("title") or "").strip()
        extract = (doc.get("extract") or "").strip()
        if doc.get("url") and title and extract and doc["url"] not in self.texts:
            self.texts[doc["url"]] = (title, extract)

    def fill(self, doc: dict) -> dict:
        if not (doc.get("title") or "").strip() or not (doc.get("extract") or "").strip():
            known = self.texts.get(doc["url"])
            if known:
                doc = {**doc, "title": known[0], "extract": known[1]}
        return doc


def slim(doc: dict) -> dict:
    return {
        "url": doc["url"],
        "title": (doc.get("title") or "").strip() or None,
        "extract": (doc.get("extract") or "").strip() or None,
    }


class PairEmitter:
    def __init__(self, lookup: TextLookup):
        self.lookup = lookup
        self.pairs: list[dict] = []
        self.seen: set[tuple] = set()
        self.rule_counts = Counter()

    def emit(self, query: str, pos: dict, negs: list[dict], rule: str, table: str, user):
        if is_nsfw(query, pos.get("url"), pos.get("title"), pos.get("extract")):
            self.rule_counts["nsfw_dropped"] += 1
            return
        for neg in negs[:PAIR_CAP]:
            if pos["url"] == neg["url"]:
                continue
            if is_nsfw(neg.get("url"), neg.get("title"), neg.get("extract")):
                self.rule_counts["nsfw_dropped"] += 1
                continue
            key = (query, pos["url"], neg["url"])
            if key in self.seen:
                continue
            self.seen.add(key)
            self.pairs.append(
                {
                    "query": query,
                    "pos": slim(self.lookup.fill(pos)),
                    "neg": slim(self.lookup.fill(neg)),
                    "rule": rule,
                    "table": table,
                    "user": user,
                }
            )
            self.rule_counts[rule] += 1


def pairs_from_curation(curation: dict, emitter: PairEmitter):
    """Adds beat originals below them; order inversions among shared results."""
    query, user = curation["query"], curation["user"]
    original, new = curation["original_results"], curation["new_results"]
    original_urls = {d["url"] for d in original}
    original_rank = {d["url"]: i for i, d in enumerate(original)}
    curated_now = {d["url"] for d in new if d["url"] not in original_urls or (d.get("state") or 0) >= APPROVED_STATE}

    for position, doc in enumerate(new):
        below = [d for d in new[position + 1 :] if d["url"] in original_urls and d["url"] not in curated_now]
        if doc["url"] not in original_urls:
            emitter.emit(query, doc, below, "add", "curations", user)
        else:
            # order inversions: originals this doc now outranks but didn't before
            passed = [d for d in below if original_rank[d["url"]] < original_rank[doc["url"]]]
            if passed:
                rule = "approve" if (doc.get("state") or 0) >= APPROVED_STATE else "move"
                emitter.emit(query, doc, passed, rule, "curations", user)


def pairs_from_user_curations(events: list[dict], emitter: PairEmitter, pointwise: list[dict]):
    """Old interface: per-action events; results list is post-action."""
    last_results: dict[tuple, list[dict]] = {}
    for event in sorted(events, key=lambda e: e["timestamp"] or ""):
        query, user = event["query"], event["user"]
        if not query:
            continue
        session, action = (user, query), event["curation"] or {}
        results = event["results"] or []
        kind = event["curation_type"]

        if kind == "curate_move":
            old, new = action.get("old_index"), action.get("new_index")
            if old is not None and new is not None and 0 <= new < old <= len(results):
                emitter.emit(query, results[new], results[new + 1 : old + 1], "move", "user_curations", user)
        elif kind == "curate_add":
            index = action.get("insert_index")
            if index is not None and 0 <= index < len(results):
                emitter.emit(query, results[index], results[index + 1 :], "add", "user_curations", user)
        elif kind == "curate_delete":
            index = action.get("delete_index")
            previous = last_results.get(session)
            if previous and index is not None and 0 <= index < len(previous):
                deleted = previous[index]
                kept = {d["url"] for d in results}
                winners = [d for d in previous[index + 1 :] if d["url"] in kept]
                for winner in winners[:PAIR_CAP]:
                    emitter.emit(query, winner, [deleted], "delete", "user_curations", user)
        elif kind == "curate_validate":
            index = action.get("validate_index")
            if action.get("is_validated") and index is not None and 0 <= index < len(results):
                doc = slim(emitter.lookup.fill(results[index]))
                if is_nsfw(query, doc["url"], doc["title"], doc["extract"]):
                    continue
                pointwise.append(
                    {"query": query, **doc, "label": 1, "rule": "validate", "table": "user_curations", "user": user}
                )

        if results:
            last_results[session] = results


TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_DESC_RE = re.compile(
    rb'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE
)
PARAGRAPH_RE = re.compile(rb"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(rb"<[^>]+>")


def extract_text(body: bytes) -> tuple[str | None, str | None]:
    def clean(raw: bytes) -> str:
        import html

        return html.unescape(TAG_RE.sub(b" ", raw).decode("utf-8", "replace")).strip()

    title_match = TITLE_RE.search(body)
    title = clean(title_match.group(1))[:200] if title_match else None
    description = META_DESC_RE.search(body)
    if description:
        return title, clean(description.group(1))[:300]
    for paragraph in PARAGRAPH_RE.findall(body)[:5]:
        text = clean(paragraph)
        if len(text) > 60:
            return title, text[:300]
    return title, None


def crawl_backfill(
    urls: list[str], cache_path: Path, limit: int | None, workers: int = 16
) -> dict[str, tuple[str, str]]:
    """Fetch missing texts over HTTP in parallel; checkpointed and resumable."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import httpx

    cache = {}
    if cache_path.exists():
        for row in load_jsonl(cache_path):
            cache[row["url"]] = row
    todo = [u for u in urls if u not in cache][:limit]
    print(f"backfill: {len(urls)} urls missing text, {len(cache)} cached, fetching {len(todo)}")

    headers = {"User-Agent": "mwmbl-judge-backfill/0.1 (+https://mwmbl.org)"}

    def fetch(url: str, client: httpx.Client) -> dict:
        row = {"url": url, "title": None, "extract": None, "status": None}
        try:
            response = client.get(url)
            row["status"] = response.status_code
            if response.status_code == 200:
                row["title"], row["extract"] = extract_text(response.content[:200_000])
        except Exception as exc:  # noqa: BLE001
            row["status"] = f"error: {type(exc).__name__}"
        return row

    with (
        gzip.open(cache_path, "at") as out,
        httpx.Client(follow_redirects=True, timeout=10.0, headers=headers) as client,
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        futures = [pool.submit(fetch, url, client) for url in todo]
        for n, future in enumerate(as_completed(futures), 1):
            row = future.result()
            out.write(json.dumps(row) + "\n")
            cache[row["url"]] = row
            if n % 200 == 0:
                out.flush()
                print(f"  fetched {n}/{len(todo)}", flush=True)

    return {u: (r["title"], r["extract"]) for u, r in cache.items() if r.get("title") and r.get("extract")}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export-dir", default="devdata/judgments_export", type=Path)
    parser.add_argument(
        "--backfill", action="store_true", help="crawl URLs whose title/extract can't be filled locally"
    )
    parser.add_argument("--backfill-limit", type=int, default=None)
    args = parser.parse_args()

    curations = load_jsonl(args.export_dir / "curations.jsonl.gz")
    user_curations = load_jsonl(args.export_dir / "user_curations.jsonl.gz")
    votes = load_jsonl(args.export_dir / "votes.jsonl.gz")

    spammers = find_spammers(curations, user_curations)
    flagged = flagged_users(curations)
    dropped_users = set(spammers) | flagged
    print(
        f"dropping {len(dropped_users)} users: "
        f"{len(spammers)} domain spammers {dict(list(spammers.items())[:5])}, "
        f"{len(flagged)} with accepted flags"
    )

    lookup = TextLookup()
    for curation in curations:
        for doc in curation["original_results"] + curation["new_results"]:
            lookup.learn(doc)
    for event in user_curations:
        for doc in event["results"] or []:
            lookup.learn(doc)
    if PASS2_POOL.exists():
        for row in load_jsonl(PASS2_POOL):
            for doc in row.get("candidates", []):
                lookup.learn(doc)
    print(f"text lookup: {len(lookup.texts)} urls with title+extract")

    emitter = PairEmitter(lookup)
    pointwise: list[dict] = []
    report = {"dropped_users": len(dropped_users), "spammer_domains": spammers}

    kept = [c for c in curations if c["user"] not in dropped_users and not is_urlish_query(c["query"])]
    urlish = sum(1 for c in curations if is_urlish_query(c["query"]))
    no_ops = 0
    for curation in kept:
        if [d["url"] for d in curation["original_results"]] == [d["url"] for d in curation["new_results"]]:
            no_ops += 1
            continue
        pairs_from_curation(curation, emitter)
    report["curations"] = {"total": len(curations), "kept": len(kept), "urlish_queries": urlish, "no_ops": no_ops}

    kept_events = [
        e for e in user_curations if e["user"] not in dropped_users and not (e["query"] and is_urlish_query(e["query"]))
    ]
    pairs_from_user_curations(kept_events, emitter, pointwise)
    report["user_curations"] = {"total": len(user_curations), "kept": len(kept_events)}

    for vote in votes:
        if vote["user"] in dropped_users or is_urlish_query(vote["query"]):
            continue
        filled = lookup.fill({"url": vote["url"], "title": None, "extract": None})
        if is_nsfw(vote["query"], vote["url"], filled.get("title"), filled.get("extract")):
            continue
        pointwise.append(
            {
                "query": vote["query"],
                **slim(filled),
                "label": 1 if vote["vote_type"] == "upvote" else -1,
                "rule": "vote",
                "table": "votes",
                "user": vote["user"],
            }
        )

    def missing_urls() -> list[str]:
        urls = set()
        for pair in emitter.pairs:
            for side in ("pos", "neg"):
                if not pair[side]["title"] or not pair[side]["extract"]:
                    urls.add(pair[side]["url"])
        urls.update(p["url"] for p in pointwise if not p["title"] or not p["extract"])
        return sorted(urls)

    if args.backfill:
        crawled = crawl_backfill(missing_urls(), args.export_dir / "backfill_cache.jsonl.gz", args.backfill_limit)
        lookup.texts.update(crawled)
        for pair in emitter.pairs:
            for side in ("pos", "neg"):
                pair[side] = slim(lookup.fill(pair[side]))
        for point in pointwise:
            filled = slim(lookup.fill(point))
            point.update(filled)

    with gzip.open(args.export_dir / "pairs.jsonl.gz", "wt") as f:
        for pair in emitter.pairs:
            f.write(json.dumps(pair) + "\n")
    with gzip.open(args.export_dir / "pointwise.jsonl.gz", "wt") as f:
        for point in pointwise:
            f.write(json.dumps(point) + "\n")

    complete = sum(1 for p in emitter.pairs if all(p[s]["title"] and p[s]["extract"] for s in ("pos", "neg")))
    report["pairs"] = {
        "total": len(emitter.pairs),
        "by_rule": dict(emitter.rule_counts),
        "distinct_queries": len({p["query"] for p in emitter.pairs}),
        "both_sides_have_text": complete,
        "urls_still_missing_text": len(missing_urls()),
    }
    report["pointwise"] = {"total": len(pointwise), "by_rule": dict(Counter(p["rule"] for p in pointwise))}
    print(json.dumps(report, indent=2))
    (args.export_dir / "transform_report.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
