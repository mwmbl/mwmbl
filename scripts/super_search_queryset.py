#!/usr/bin/env python3
"""Per-source "home query" set for Super Search offline training/evaluation.

Builds ``devdata/ss_source_queries.json`` (``{source: [queries]}``): for every
registered source, 5-10 realistic user queries that this source should answer
*best*. Together with a dense judge-reward matrix (every source runs on every
query — other sources' home queries are natural negatives) this replaces the
Google-autocomplete-derived dataset for warm-starting the xgb source model.

The LLM generation is driven externally (a subagent batch per call), following
the checkpointed dump-batch/merge pattern of ``llm_relabel_pass1_run.py``:

- ``--status``        progress: sources with/without queries.
- ``--dump-batch N``  print catalog blocks for the next N un-covered sources —
                      name, domain, field, description, plus grounding terms
                      probed live from the source's own search results — ready
                      to hand to the generator.
- ``--no-probe``      skip the live grounding probe (offline dump).
- ``--merge FILE``    validate a generated ``{source: [queries]}`` JSON file
                      and fold it into the checkpoint.

Usage::
    DJANGO_SETTINGS_MODULE=mwmbl.settings_dev uv run python \
        scripts/super_search_queryset.py --dump-batch 30
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import django

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from django.conf import settings  # noqa: E402

CHECKPOINT = REPO_ROOT / "devdata" / "ss_source_queries.json"
CATALOG = REPO_ROOT / "devdata" / "llm_relabel" / "source_catalog.json"

MIN_QUERIES, MAX_QUERIES = 5, 10
PROBE_SEEDS = 3  # description-derived probe queries per source
PROBE_LIMIT = 5  # results per probe
GROUNDING_TERMS = 15

_WORD_RE = re.compile(r"[a-z][a-z0-9'-]{3,}")
_STOPWORDS = frozenset(
    "this that with from into over under have been will your their them they "
    "these those what when where which while about after before other others "
    "more most some such only very also than then there here each every and "
    "the for are was were has had can could should would may might must not "
    "its his her our you all any but out off own same too who whom does did "
    "site sites page pages website websites online free best".split()
)

GENERATION_BRIEF = """\
For each source below, write {min_q}-{max_q} realistic user search queries that this
source should answer BEST among a general web search's sources (its "home"
queries). Rules:
- Queries a real user would type: mostly lowercase, 1-6 words, no URLs, no
  quotes, no site: operators. Mix short/ambiguous with specific ones.
- Ground them in what the source actually covers (see description + grounding
  terms from its live results); include some long-tail/specific queries, not
  only the obvious head terms.
- For navigational sources (homepage, wikidata_official) queries are brand /
  organisation / person names.
- Output a single JSON object {{"source_name": ["query", ...], ...}} covering
  every source in the batch. Merge with:
      uv run python scripts/super_search_queryset.py --merge FILE
"""


def load_checkpoint() -> dict[str, list[str]]:
    if CHECKPOINT.exists():
        return json.loads(CHECKPOINT.read_text())
    return {}


def save_checkpoint(data: dict[str, list[str]]) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps(dict(sorted(data.items())), indent=1))


def all_sources() -> list[str]:
    from mwmbl.tinysearchengine.super_search_sources import SOURCES

    return list(SOURCES.keys())


def source_info(name: str) -> tuple[str, str, str]:
    """(domain, field, description) from the registry + LLM source catalog + shortlist."""
    from mwmbl.tinysearchengine.super_search_select.registry import (
        _load_shortlist,
        get_meta,
    )

    meta = get_meta(name)
    catalog = json.loads(CATALOG.read_text()) if CATALOG.exists() else {}
    desc = catalog.get(name, {}).get("description", "")
    field = catalog.get(name, {}).get("field", meta.field)
    if not desc:
        desc = _load_shortlist().get(meta.domain, {}).get("reason", "")
    return meta.domain, field, desc


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS]


async def _probe_source(name: str, seeds: list[str]) -> list[str]:
    """Query the source with description-derived seeds; return its top result terms."""
    import httpx

    from mwmbl.tinysearchengine.super_search_sources import SOURCES

    fn = SOURCES[name]
    timeout = settings.SUPER_SEARCH_PER_SOURCE_TIMEOUT
    counts: Counter = Counter()
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers={"User-Agent": "mwmbl-super-search-eval/0.1"}
    ) as client:
        for seed in seeds:
            try:
                docs = await asyncio.wait_for(fn(client, seed, PROBE_LIMIT), timeout=timeout)
            except Exception:
                continue
            for d in docs:
                counts.update(_content_words(f"{d.title or ''} {d.extract or ''}"))
    return [w for w, _ in counts.most_common(GROUNDING_TERMS)]


def dump_batch(n: int, probe: bool) -> None:
    done = load_checkpoint()
    todo = [s for s in all_sources() if s not in done][:n]
    if not todo:
        print("all sources covered.")
        return
    print(GENERATION_BRIEF.format(min_q=MIN_QUERIES, max_q=MAX_QUERIES))
    for name in todo:
        domain, field, desc = source_info(name)
        print(f"### {name}")
        print(f"domain: {domain}   field: {field}")
        print(f"description: {desc or '(none)'}")
        if probe:
            seeds = list(dict.fromkeys(_content_words(desc)))[:PROBE_SEEDS] or [field]
            terms = asyncio.run(_probe_source(name, seeds))
            print(f"grounding (live results for seeds {seeds}): {', '.join(terms) if terms else '(no results)'}")
        print()
    print(
        f"[{len(todo)} sources dumped; {len(done)} done, "
        f"{len(all_sources()) - len(done) - len(todo)} remaining after these]"
    )


_URLISH_RE = re.compile(r"https?://|www\.", re.I)


def merge(path: str) -> None:
    generated = json.loads(Path(path).read_text())
    if not isinstance(generated, dict):
        raise ValueError("merge file must be a JSON object {source: [queries]}")
    done = load_checkpoint()
    known = set(all_sources())
    for name, queries in generated.items():
        if name not in known:
            raise ValueError(f"unknown source {name!r}")
        if name in done:
            raise ValueError(f"source {name!r} already has queries; refusing to overwrite")
        deduped = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        if not MIN_QUERIES <= len(deduped) <= MAX_QUERIES:
            raise ValueError(f"{name!r}: {len(deduped)} queries after dedup, need {MIN_QUERIES}-{MAX_QUERIES}")
        for q in deduped:
            if _URLISH_RE.search(q) or len(q) > 80:
                raise ValueError(f"{name!r}: bad query {q!r}")
        done[name] = deduped
    save_checkpoint(done)
    print(f"merged {len(generated)} sources; checkpoint now covers {len(done)}/{len(known)} sources.")


def status() -> None:
    done = load_checkpoint()
    sources = all_sources()
    missing = [s for s in sources if s not in done]
    n_queries = sum(len(v) for v in done.values())
    print(f"{len(done)}/{len(sources)} sources covered, {n_queries} queries total.")
    if missing:
        print("missing:", ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true")
    group.add_argument("--dump-batch", type=int, metavar="N")
    group.add_argument("--merge", metavar="FILE")
    parser.add_argument("--no-probe", action="store_true", help="skip the live grounding probe when dumping")
    args = parser.parse_args()
    if args.status:
        status()
    elif args.dump_batch:
        dump_batch(args.dump_batch, probe=not args.no_probe)
    else:
        merge(args.merge)


if __name__ == "__main__":
    main()
