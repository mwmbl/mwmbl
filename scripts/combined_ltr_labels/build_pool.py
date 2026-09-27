"""The Combined Search serving pool for the 849 LLM-labelled queries, and which of it is unjudged.

For each query in `devdata/llm_relabel/pass1_intents.jsonl` this takes:

- Staan's results, from the Pass-2 pool (the rows tagged `staan`), all of them;
- the index's top DEPTH candidates under the shipped Combined Search model
  (`CombinedLTRRanker` + MMR, `RemoteIndex` against api.mwmbl.org), with no Staan results
  passed in. Staan's results are labelled whatever their rank, so the only thing their rank
  would change here is how the index candidates interleave with them, not which ones reach
  the top DEPTH.

Pairs already in `pass3_judgments.jsonl` are not judged again. The rest go to
`make_batches.py`. Writes `devdata/combined_ltr_labels/pool.json`: query -> list of
{url, title, extract, state, score, source, judged}.
"""

import json
import os
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()
from django.conf import settings

from mwmbl.rankeval.evaluation.evaluate_ranker import DummyCompleter
from mwmbl.rankeval.evaluation.remote_index import RemoteIndex
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker

DEPTH = 30
RELABEL = Path("devdata/llm_relabel")
OUT = Path("devdata/combined_ltr_labels/pool.json")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in open(path)]


def run():
    queries = [record["query"] for record in read_jsonl(RELABEL / "pass1_intents.jsonl")]
    staan = {
        record["query"]: [c for c in record["candidates"] if "staan" in c["pools"]]
        for record in read_jsonl(RELABEL / "pass2_pool.jsonl")
    }
    judged = {(j["query"], j["url"]) for j in read_jsonl(RELABEL / "pass3_judgments.jsonl")}

    model = RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))
    ranker = MMRRanker(CombinedLTRRanker(RemoteIndex(), DummyCompleter(), model))

    pool = json.loads(OUT.read_text()) if OUT.exists() else {}
    for i, query in enumerate(queries):
        if query in pool:
            continue
        rows = {}
        for c in staan[query]:
            rows[c["url"]] = {**{k: c[k] for k in ("url", "title", "extract", "state", "score")}, "source": "staan"}
        for doc in ranker.search(query, [], False)[:DEPTH]:
            if doc.url in rows:
                rows[doc.url]["source"] = "both"
                continue
            rows[doc.url] = {
                "url": doc.url,
                "title": doc.title,
                "extract": doc.extract,
                "state": doc.state,
                "score": doc.score,
                "source": "index",
            }
        for row in rows.values():
            row["judged"] = (query, row["url"]) in judged
        pool[query] = list(rows.values())
        if i % 50 == 0:
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(pool))
            print(i, query, len(rows), sum(not r["judged"] for r in rows.values()), flush=True)
    OUT.write_text(json.dumps(pool))
    rows = [row for query_rows in pool.values() for row in query_rows]
    print(f"{len(pool)} queries, {len(rows)} pairs, {sum(not r['judged'] for r in rows)} unjudged")


if __name__ == "__main__":
    run()
