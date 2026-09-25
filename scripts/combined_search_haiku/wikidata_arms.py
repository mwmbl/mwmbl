"""List the Wikidata arms' URLs that no earlier judgment covers, with anchors to re-grade.

Writes engb/wikidata_to_judge.json: per query, the ungraded URLs any arm in
mwmbl.rankeval.evaluation.wikidata_fill_report puts in its top ten, and up to four URLs both
judges already graded, to be shuffled in as anchors.
"""

import json
import random

from mwmbl.rankeval.evaluation.haiku_arms_report import EVAL_DIR, load_json, load_judgments
from mwmbl.rankeval.evaluation.wikidata_fill_report import (
    ltr_and_wikipedia_by_minilm,
    wikidata_arms,
    wikidata_candidates,
)

NUM_ANCHORS = 4

rows = load_json("engb/rows-0.05.json")
candidates, ltr_and_wiki = wikidata_candidates(), ltr_and_wikipedia_by_minilm()
relevance, pass3 = load_judgments("haiku/relevance_engb.jsonl"), load_judgments("haiku/pass3_engb.jsonl")
new, anchors = {}, {}
for row in rows:
    query = row["query"]
    if not candidates[query]:
        continue
    urls = {url for arm in wikidata_arms(row, candidates[query], ltr_and_wiki[query]).values() for url in arm}
    ungraded = sorted(url for url in urls if url not in relevance.get(query, {}) or url not in pass3.get(query, {}))
    if ungraded:
        new[query] = ungraded
        both = sorted(set(relevance.get(query, {})) & set(pass3.get(query, {})))
        anchors[query] = random.Random(query).sample(both, min(NUM_ANCHORS, len(both)))
(EVAL_DIR / "engb/wikidata_to_judge.json").write_text(json.dumps({"new": new, "anchors": anchors}, indent=0))
print("queries", len(new), "new urls", sum(map(len, new.values())), "anchors", sum(map(len, anchors.values())))
