"""Step 3 of the structural-fixes study: sample unjudged results to grade.

The designs fill the top ten with results #454 never judged. This samples, blind and
shuffled, the unjudged results the designs bring in ("newcomer"), the unjudged results
already in the baseline top ten ("baseline"), and judged baseline results to calibrate the
grader against Haiku ("calibration").

    structural_grade.py dump     # writes to_grade.txt and its key to the work directory
    structural_grade.py load G   # G holds "id:grade,..."; writes GRADES_PATH

The committed grades were written by Claude with the UK-relevance prompt in
haiku/prompts/relevance_engb.txt, from the title, URL and snippet shown.
"""

import json
import random
import sys

from structural_common import GRADES_PATH, load_grades, load_work, save_work, term_cache, work_path

from mwmbl.rankeval.evaluation.haiku_arms_report import normalise_url

SAMPLES = {"newcomer": 160, "baseline": 90, "calibration": 60}


def dump():
    lists, grades = load_work("lists.json"), load_grades()
    baseline = lists["baseline"]
    in_baseline = {(query, normalise_url(url)) for query, urls in baseline.items() for url in urls}
    text = {}
    for documents in [*load_work("deep_pool.json").values(), *term_cache.values()]:
        for d in documents:
            text.setdefault(normalise_url(d["url"]), d)
    for target in load_work("targets.json")["targets"]:
        text.setdefault(normalise_url(target["url"]), target["stored"])

    newcomers = {
        (query, normalise_url(url))
        for draws in lists["deep"].values()
        for draw in draws
        for query, urls in draw.items()
        for url in urls
        if (query, normalise_url(url)) not in in_baseline and normalise_url(url) not in grades[query]
    }
    baseline_unjudged = {(query, key) for query, key in in_baseline if key not in grades[query] and key in text}
    baseline_judged = {(query, key) for query, key in in_baseline if key in grades[query] and key in text}
    rng = random.Random(7)
    items = [
        (kind, query, key)
        for kind, population in (
            ("newcomer", newcomers),
            ("baseline", baseline_unjudged),
            ("calibration", baseline_judged),
        )
        for query, key in rng.sample(sorted(population), SAMPLES[kind])
    ]
    rng.shuffle(items)
    save_work(
        "to_grade_key.json", [{"id": i, "kind": k, "query": q, "key": key} for i, (k, q, key) in enumerate(items)]
    )
    with open(work_path("to_grade.txt"), "w") as output:
        for i, (_, query, key) in enumerate(items):
            d = text[key]
            output.write(
                f"[{i}] Q: {query}\n    T: {d['title']}\n    U: {d['url']}\n    S: {(d['extract'] or '')[:220]}\n"
            )


def load(path: str):
    given = dict(tuple(map(int, pair.split(":"))) for pair in open(path).read().strip().split(","))
    grades = load_grades()
    GRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GRADES_PATH, "w") as output:
        for item in load_work("to_grade_key.json"):
            haiku = grades[item["query"]].get(item["key"])
            record = {
                "kind": item["kind"],
                "query": item["query"],
                "key": item["key"],
                "grade": given[item["id"]],
                "haiku": haiku,
            }
            output.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "dump":
        dump()
    else:
        load(sys.argv[2])
