"""Score Combined Search's en-gb candidates with Jev (TypeSafe), for jev_arms_report.

Reads ``engb/jev_candidates.json`` (written by ``jev_arms_report --export-candidates``) and
writes ``haiku/jev_<variant>.jsonl``, one ``{"query", "url", "score"}`` per candidate.
One request per query in both variants:

- ``pointwise``: one Score question per candidate, on the UK-relevance rubric the Haiku
  judge uses. The query is the state; each candidate goes in its question's instructions, so
  Jev evaluates each one in isolation. The score is the probability-weighted level, 0-3.
- ``listwise``: one Choice over every candidate, "which result best satisfies the query?".
  The score is the option's probability.

Raw responses are cached in ``$HAIKU_WORK_DIR/jev/`` so an interrupted run resumes.

Usage::

    JEV_API_KEY=... uv run --no-sync python scripts/combined_search_haiku/jev_score.py pointwise
"""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1
from pathlib import Path

import requests

EVAL_DIR = Path("devdata/combined_providers_eval")
WORK_DIR = Path(os.environ.get("HAIKU_WORK_DIR", EVAL_DIR / "haiku_work")) / "jev"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
RETRY_STATUSES = {429, 529}

LEVELS = [
    "Irrelevant: off-topic, wrong entity, spam, or broken.",
    "Marginal: on-topic-ish but thin or tangential, the wrong sense of an ambiguous query, an SEO or "
    "doorway page, or a page for another country where the answer depends on the country.",
    "Good: relevant and useful, but partial, secondary, or less authoritative.",
    "Excellent: directly satisfies what the searcher most likely wants - the official site for a "
    "navigational query, a thorough direct answer to a question, an authoritative page on exactly "
    "that entity, the right local or transactional page, a current report for a news query.",
]


def result_fields(candidate: dict) -> dict:
    return {"title": candidate["title"], "url": candidate["url"], "snippet": candidate["extract"]}


def pointwise_request(query: str, candidates: list[dict]) -> dict:
    questions = {
        f"r{i}": {
            "type": "score",
            "instructions": {
                "result": result_fields(candidate),
                "question": "How well does the search result `result` satisfy `query`, for `searcher`?",
            },
            "criteria": LEVELS,
        }
        for i, candidate in enumerate(candidates)
    }
    return {"state": {"query": query, "searcher": "a web searcher in the United Kingdom"}, "questions": questions}


def listwise_request(query: str, candidates: list[dict]) -> dict:
    results = {f"r{i}": result_fields(candidate) for i, candidate in enumerate(candidates)}
    question = {
        "type": "choice",
        "instructions": "Which of `results` best satisfies `query`, for `searcher`?",
        "criteria": {key: None for key in results},
    }
    state = {"query": query, "searcher": "a web searcher in the United Kingdom", "results": results}
    return {"state": state, "questions": {"best": question}}


def scores_from(variant: str, response: dict, num_candidates: int) -> list[float]:
    answers = response["answers"]
    if variant == "pointwise":
        return [answers[f"r{i}"]["score"] for i in range(num_candidates)]
    probabilities = answers["best"]["probabilities"]
    return [probabilities[f"r{i}"] for i in range(num_candidates)]


def call(body: dict, api_key: str) -> dict:
    for attempt in range(6):
        response = requests.post(
            ENDPOINT, json={"model": MODEL, **body}, headers={"Authorization": f"Bearer {api_key}"}, timeout=120
        )
        if response.status_code not in RETRY_STATUSES:
            response.raise_for_status()
            return response.json()
        time.sleep(2**attempt)
    response.raise_for_status()
    raise RuntimeError("unreachable")


def score_query(variant: str, query: str, candidates: list[dict], api_key: str) -> tuple[dict, float]:
    build = pointwise_request if variant == "pointwise" else listwise_request
    body = build(query, candidates)
    cache_path = WORK_DIR / variant / (sha1(json.dumps(body, sort_keys=True).encode()).hexdigest() + ".json")
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        return cached["response"], cached["seconds"]
    start = time.monotonic()
    response = call(body, api_key)
    seconds = time.monotonic() - start
    cache_path.write_text(json.dumps({"query": query, "seconds": seconds, "response": response}))
    return response, seconds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("variant", choices=["pointwise", "listwise"])
    parser.add_argument("--limit", type=int, help="score only the first N queries")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    api_key = os.environ["JEV_API_KEY"]
    candidates = json.loads((EVAL_DIR / "engb/jev_candidates.json").read_text())
    queries = list(candidates)[: args.limit]
    (WORK_DIR / args.variant).mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda q: score_query(args.variant, q, candidates[q], api_key), queries))

    lines, latencies, input_tokens = [], [], 0
    for query, (response, seconds) in zip(queries, results):
        scores = scores_from(args.variant, response, len(candidates[query]))
        for candidate, score in zip(candidates[query], scores):
            lines.append(json.dumps({"query": query, "url": candidate["url"], "score": score}))
        latencies.append(seconds)
        input_tokens += response["usage"]["input_tokens"]
    (EVAL_DIR / f"haiku/jev_{args.variant}.jsonl").write_text("\n".join(lines) + "\n")

    latencies.sort()
    print(
        f"{len(queries)} queries, {len(lines)} candidates, {input_tokens} input tokens. "
        f"Latency p50 {latencies[len(latencies) // 2]:.2f}s, p90 {latencies[int(len(latencies) * 0.9)]:.2f}s."
    )


if __name__ == "__main__":
    main()
