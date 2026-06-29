"""
LLM relabel — Pass 1 RUNNER: query analysis (intent + source selection),
checkpointed and resumable.

The Haiku judging itself is driven externally (a subagent batch per call). This
script handles ordering, batching, and merging so the pass can pause/resume and
stop early with an unbiased subset:

- ``--status``         show progress + intent distribution from the checkpoint.
- ``--dump-batch N``   print the next N un-judged queries (stratified-random
                       order) plus the catalog block, ready to hand to a judge.
- ``--merge FILE``     parse a raw judge-output file (lines
                       ``query || intent || src1,src2``), validate, apply the
                       intent->source augmentation, append to the checkpoint.

Ordering is stratified-random by token-count bucket (1 / 2 / 3+ words),
round-robin interleaved over a seeded shuffle, so any prefix is balanced across
query shapes and independent of the existing LTR (avoids circularity).

Usage::
    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev DATABASE_URL="postgres://daoud@" \
        uv run python scripts/llm_relabel_pass1_run.py --dump-batch 200
"""
import json
import os
from argparse import ArgumentParser
from collections import Counter, defaultdict

import django
import numpy as np
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from mwmbl.rankeval.paths import RANKINGS_DATASET_TRAIN_PATH  # noqa: E402
from scripts.llm_relabel_pass1 import INTENT_SOURCES, augment_sources  # noqa: E402

CHECKPOINT = "devdata/llm_relabel/pass1_intents.jsonl"
SEED = 42
INTENTS = {"navigational", "entity", "informational", "transactional",
           "local", "news", "tool", "ambiguous"}

# Curated catalog the judge chooses from (matches the validated Pass-1 prompt).
CATALOG_BLOCK = """\
mwmbl [general]: Mwmbl's own crawled web index — general web pages on any topic.
hn [tech]: Hacker News — tech, startups, programming, science.
github [code]: GitHub — source-code repositories and software projects.
stackexchange [qa]: Stack Exchange — programming/technical Q&A.
arxiv [academia]: arXiv — academic preprints.
pypi [code]: PyPI — Python package index.
imdb [entertainment]: IMDb — films, TV shows, actors, entertainment.
nhs [health]: NHS — UK consumer health: conditions, symptoms, medicines, dosages.
openstreetmap_org [maps-places]: OpenStreetMap — places, towns, addresses, local points of interest.
wikidata_official [navigational]: Official website of a named brand/company/org/person (via Wikidata).
homepage [navigational]: Best-guess official homepage for a brand/site name.
guardian [news]: The Guardian — UK/world news, politics, sport, current events.
genius_com [music]: song lyrics and music.
musicbrainz_org [music]: music metadata / artists / releases.
gutenberg [books-literature]: public-domain books.
wikisource_org [books-literature]: free-license texts.
www_etymonline_com [books-literature]: etymology / word origins.
wiktionary [other]: dictionary definitions.
www_gov_uk [law-government]: UK government services and info.
www_legislation_gov_uk [law-government]: UK legislation text.
minecraft_wiki [gaming]: Minecraft wiki.
bulbapedia_bulbagarden_net [gaming]: Pokémon wiki.
itch_io [gaming]: indie games.
developer_mozilla_org [programming]: web dev docs (MDN).
docs_djangoproject_com [programming]: Django docs.
wiki_archlinux_org [tech]: Linux system administration.
www_howtogeek_com [tech]: consumer tech how-to guides.
quantamagazine_org [science]: science journalism.
www_space_com [science]: space/astronomy news.
phys_org [science]: physics/science news.
theconversation_com [news-politics]: expert commentary on current issues."""

JUDGE_INSTRUCTIONS = """\
You are a query-analysis judge for a search engine. For EACH query: (1) assign \
exactly ONE intent, (2) pick 0-5 sources from the catalog (exact names) that \
would best serve it, or "none". Only pick a source if it plausibly holds a \
strongly relevant result; "none"/just "mwmbl" is fine and common.

Intents:
- navigational: wants a specific known site/brand/company/org/person's official page.
- entity: wants info ABOUT a named person/work/place/thing (not necessarily its official site).
- informational: general how/what/why or topic-learning query.
- transactional: wants to buy/book/download/obtain something.
- local: wants a place/business near a location, maps, addresses, directions.
- news: wants current events / recent developments.
- tool: wants an instant utility/answer — converter, calculator, checker, live score/data.
- ambiguous: genuinely unclear, could be several with no dominant reading.

Output ONE line per query, nothing else:
query || intent || source1,source2   (or "none")"""


def _ordered_queries() -> list[str]:
    queries = pd.read_csv(RANKINGS_DATASET_TRAIN_PATH)["query"].dropna().unique().tolist()
    buckets = defaultdict(list)
    for q in queries:
        n = len(str(q).split())
        buckets[1 if n <= 1 else 2 if n == 2 else 3].append(q)
    rng = np.random.default_rng(SEED)
    for b in buckets.values():
        rng.shuffle(b)
    # round-robin interleave so any prefix is balanced across query shapes
    out, idx = [], {k: 0 for k in buckets}
    while len(out) < len(queries):
        for k in (1, 2, 3):
            if idx[k] < len(buckets[k]):
                out.append(buckets[k][idx[k]])
                idx[k] += 1
    return out


def _load_done() -> dict:
    done = {}
    if os.path.exists(CHECKPOINT):
        for line in open(CHECKPOINT):
            line = line.strip()
            if line:
                rec = json.loads(line)
                done[rec["query"]] = rec
    return done


def cmd_status():
    done = _load_done()
    total = len(_ordered_queries())
    print(f"judged {len(done)} / {total} queries ({CHECKPOINT})")
    if done:
        dist = Counter(r["intent"] for r in done.values())
        print("intent distribution:")
        for k, n in dist.most_common():
            print(f"  {k:14s} {n}")
        srcs = Counter(s for r in done.values() for s in r["sources"])
        print("source routing (post-augmentation):")
        for s, n in srcs.most_common():
            print(f"  {s:20s} {n}")


def cmd_dump_batch(n: int):
    done = set(_load_done())
    todo = [q for q in _ordered_queries() if q not in done][:n]
    print(JUDGE_INSTRUCTIONS)
    print("\n=== SOURCE CATALOG (name [field]: description) ===")
    print(CATALOG_BLOCK)
    print("\n=== QUERIES ===")
    for q in todo:
        print(q)


def cmd_merge(path: str):
    done = _load_done()
    added = skipped = bad = 0
    with open(CHECKPOINT, "a") as out:
        for line in open(path):
            line = line.strip()
            if not line or "||" not in line:
                continue
            parts = [p.strip() for p in line.split("||")]
            if len(parts) != 3:
                bad += 1
                continue
            query, intent, src = parts
            if intent not in INTENTS:
                bad += 1
                continue
            if query in done:
                skipped += 1
                continue
            picks = [] if src.lower() == "none" else [s.strip() for s in src.split(",") if s.strip()]
            rec = {"query": query, "intent": intent,
                   "haiku_sources": picks,
                   "sources": augment_sources(intent, picks)}
            out.write(json.dumps(rec) + "\n")
            done[query] = rec
            added += 1
    print(f"merged {path}: +{added} added, {skipped} dup-skipped, {bad} malformed")


def main():
    parser = ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dump-batch", type=int, metavar="N")
    parser.add_argument("--merge", metavar="FILE")
    args = parser.parse_args()
    if args.merge:
        cmd_merge(args.merge)
    elif args.dump_batch:
        cmd_dump_batch(args.dump_batch)
    else:
        cmd_status()


if __name__ == "__main__":
    main()
