"""The training queries' Staan results at market en-gb, judged, for retraining on them.

The training data's Staan results (`pass2_staan.jsonl`) were fetched at the `STAAN_MARKET`
default, en-us: 6.8% of them are on .uk hosts, against 30% of en-gb Staan's on the en-gb
eval. Their tail holds US pages for UK queries (Tulsa police, the California DMV), so the
model learns to displace Staan's lower ranks. On en-gb, where Staan's tail is much better,
those swaps lose. This fetches the 849 queries' Staan results again at en-gb, so the model
can be trained on the Staan it is served with.

    collect      en-gb Staan results for every query -> pass2_staan_engb.jsonl. Resumable.
    batches      Pass-3 batches (with Pass-1 intent) for the pairs no judgment covers, plus anchors
    consolidate  judge output -> pass3_staan_engb.jsonl

Run `collect` with DJANGO_SETTINGS_MODULE=scripts.combined_search_haiku.settings_engb, whose
external cache is separate from the en-us one (the cache is keyed by query alone).
"""

import json
import os
import random
import sys
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "scripts.combined_search_haiku.settings_engb")
django.setup()
import numpy as np
from django.conf import settings

sys.path.insert(0, "scripts")
from llm_relabel_pass3_judge import EXTRACT_CHARS, JUDGE_PROMPT

from mwmbl.indexer.external_cache import get_cached_external_results
from mwmbl.tinysearchengine.indexer import DocumentSource
from mwmbl.tinysearchengine.staan import get_staan_results
from scripts.combined_ltr_labels.engb_eval import LINE

LABELS = Path("devdata/combined_ltr_labels")
STAAN_ENGB = LABELS / "pass2_staan_engb.jsonl"
JUDGMENTS = LABELS / "pass3_staan_engb.jsonl"
WORK = LABELS / "staan_engb_work"
POOL = Path("devdata/llm_relabel/pass2_pool.jsonl")
INTENTS = Path("devdata/llm_relabel/pass1_intents.jsonl")
ORIGINAL = Path("devdata/llm_relabel/pass3_judgments.jsonl")
SERVING_POOL_JUDGMENTS = LABELS / "pass3_serving_pool.jsonl"
ANCHORS = 2
CANDIDATES_PER_BATCH = 250


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in open(path)] if path.exists() else []


def collect():
    assert settings.STAAN_MARKET == "en-gb", settings.STAAN_MARKET
    done = {record["query"] for record in read_jsonl(STAAN_ENGB)}
    todo = [record["query"] for record in read_jsonl(POOL) if record["query"] not in done]
    print(f"collecting {len(todo)} queries")
    failed = 0
    with open(STAAN_ENGB, "a") as out:
        for i, query in enumerate(todo, 1):
            results = get_staan_results(query)
            # A failure isn't cached, so a miss straight after the call means the fetch failed;
            # leaving it out of the checkpoint makes a re-run retry it.
            if get_cached_external_results(DocumentSource.STAAN, query) is None:
                failed += 1
                print(f"[{i}/{len(todo)}] {query!r}: FAILED", flush=True)
                if failed >= 5 and failed == i:
                    raise SystemExit("every fetch is failing: check the key and the quota")
                continue
            record = {
                "query": query,
                "results": [
                    {"url": d.url, "title": d.title, "extract": d.extract, "rank": rank}
                    for rank, d in enumerate(results)
                ],
            }
            out.write(json.dumps(record) + "\n")
            out.flush()
            if i % 50 == 0:
                print(f"[{i}/{len(todo)}]", flush=True)
    print(f"done, {failed} failed")


def known_grades() -> dict[tuple[str, str], dict]:
    grades = {(j["query"], j["url"]): j for j in read_jsonl(ORIGINAL)}
    for j in read_jsonl(SERVING_POOL_JUDGMENTS):
        if not j.get("anchor"):
            grades.setdefault((j["query"], j["url"]), j)
    return grades


def batches():
    staan = read_jsonl(STAAN_ENGB)
    intents = {record["query"]: record["intent"] for record in read_jsonl(INTENTS)}
    original = {(j["query"], j["url"]) for j in read_jsonl(ORIGINAL)}
    graded = known_grades()
    blocks, manifest, next_id = [], {}, 1
    for i, record in enumerate(sorted(staan, key=lambda r: r["query"])):
        query = record["query"]
        new = [r for r in record["results"] if (query, r["url"]) not in graded]
        if not new:
            continue
        # Anchors from the original Pass-3 judgments, preferring this query's en-us Staan
        # results, so drift is measured on the scale the training labels use.
        rng = random.Random(i)
        anchors = [r for r in record["results"] if (query, r["url"]) in original]
        rng.shuffle(anchors)
        chosen = [(r, False) for r in new] + [(r, True) for r in anchors[:ANCHORS]]
        rng.shuffle(chosen)
        lines = [f"\n=== QUERY: {query}\n=== INTENT: {intents[query]}"]
        for r, is_anchor in chosen:
            manifest[next_id] = [query, r["url"], is_anchor]
            extract = (r["extract"] or "").replace("\n", " ")[:EXTRACT_CHARS]
            lines.append(f"{next_id}. {r['title'] or '(no title)'} — {r['url']}\n   {extract}")
            next_id += 1
        blocks.append((len(chosen), "\n".join(lines)))

    WORK.mkdir(parents=True, exist_ok=True)
    groups: list[list[str]] = [[]]
    size = 0
    for count, block in blocks:
        if size + count > CANDIDATES_PER_BATCH and groups[-1]:
            groups.append([])
            size = 0
        groups[-1].append(block)
        size += count
    for b, group in enumerate(groups):
        (WORK / f"batch_{b:02d}.txt").write_text(JUDGE_PROMPT + "\n".join(group) + "\n")
    (WORK / "manifest.json").write_text(json.dumps(manifest))
    anchors = sum(is_anchor for *_, is_anchor in manifest.values())
    print(f"{len(blocks)} queries, {len(manifest)} candidates ({anchors} anchors) in {len(groups)} batches")


def consolidate():
    manifest = {int(k): v for k, v in json.loads((WORK / "manifest.json").read_text()).items()}
    grades, judges = {}, {}
    for path in sorted(WORK.glob("out_*.txt")):
        for line in path.read_text().splitlines():
            match = LINE.match(line)
            if not match:
                continue
            cid, relevance, ethos, overall = map(int, match.groups())
            assert cid in manifest and cid not in grades, f"{path.name}: bad or repeated id {cid}"
            assert relevance <= 3 and ethos <= 3 and overall <= 10, f"{path.name}: out of range: {line}"
            grades[cid] = (relevance, ethos, overall)
            judges[cid] = path.stem.removeprefix("out_")
    missing = sorted(set(manifest) - set(grades))
    assert not missing, f"{len(missing)} ids ungraded, first {missing[:10]}"
    original = {(j["query"], j["url"]): j for j in read_jsonl(ORIGINAL)}
    drift = []
    with open(JUDGMENTS, "w") as f:
        for cid in sorted(manifest):
            query, url, is_anchor = manifest[cid]
            relevance, ethos, overall = grades[cid]
            record = {"query": query, "url": url, "relevance": relevance, "ethos": ethos, "overall": overall}
            record["judge"] = judges[cid]
            if is_anchor:
                record["anchor"] = True
                drift.append(overall - original[(query, url)]["overall"])
            f.write(json.dumps(record) + "\n")
    new = [grades[cid][2] for cid in manifest if not manifest[cid][2]]
    print(f"{len(new)} new (mean overall {np.mean(new):.2f}), {len(drift)} anchors; drift {np.mean(drift):+.2f}")


if __name__ == "__main__":
    {"collect": collect, "batches": batches, "consolidate": consolidate}[sys.argv[1]]()
