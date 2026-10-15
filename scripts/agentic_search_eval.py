"""Agentic search eval: live Combined Search vs Brave as an LLM's web_search tool.

An LLM answers Search Arena prompts with a single web_search tool backed by one engine or
the other, then judges compare the two final answers side by side, in both orders. This
measures what matters to an agent: whether the answer it writes is better, not how a
results page looks to a person.

The answerer sees each result's title, URL and snippet, and never fetches pages, so the
two arms differ only in the search API. Judging is done by Claude Sonnet subagents
(`.claude/agents/answer-judge-sonnet.md`, Read/Write only), one per batch file, writing
WORK/judge/out_NN.txt for batch_NN.txt. The two orders of a comparison are in different
batches, so no judge sees both.

    sample PARQUET WORK            100 English, first-turn, search-shaped prompts from
                                   lmarena-ai/search-arena-24k -> WORK/queries.json
                                   (needs duckdb: `uv run --with duckdb ...`)
    answer WORK ARM MODEL          answers for ARM (combined | brave) -> WORK/answers_ARM.jsonl
    batches WORK                   judge batches and a manifest -> WORK/judge/
    consolidate WORK               WORK/judge/out_*.txt -> WORK/judgments.jsonl
    report WORK                    preference for Combined with a 95% CI and breakdowns

`answer` calls the live endpoints, so every Combined Search call counts against the key's
quota and indexes Staan's results. It is resumable: answered queries are skipped on rerun.
Needs REGOLO_API_KEY, MWMBL_SEARCH_API_KEY and BRAVE_SEARCH_API_KEY.
"""

import json
import os
import random
import re
import threading
import time
from argparse import ArgumentParser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import requests

NUM_QUERIES = 100
SAMPLE_SEED = 20261015
# Prompts that aren't a search: writing, rewriting, chit-chat.
EXCLUDED_INTENTS = ("Creative Generation", "Text Processing", "Other")
MAX_SEARCHES = 4
NUM_RESULTS = 10
CONCURRENCY = 6
LLM_URL = "https://api.regolo.ai/v1/chat/completions"
COMBINED_URL = "https://api.mwmbl.org/api/v2/combined-search/"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
# The Brave plan allows one request per second.
BRAVE_MIN_INTERVAL_SECONDS = 1.1
ARMS = ("combined", "brave")
COMPARISONS_PER_BATCH = 10
BOOTSTRAP_SAMPLES = 5000
STRENGTHS = {"slight": 1, "clear": 2, "strong": 3}
TAG = re.compile(r"<[^>]+>")
TODAY = date.today().isoformat()  # noqa: DTZ011 - the answerer only needs the calendar date

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web. Returns up to 10 results, each with a title, URL and snippet.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query"}},
                "required": ["query"],
            },
        },
    }
]

ANSWER_SYSTEM = f"""\
You are a helpful assistant with a web_search tool. Today's date is {TODAY}.
Use web_search to find current, reliable information before answering; you may search up to
{MAX_SEARCHES} times, refining your queries as needed. Ground your answer in what the search results
say, and cite the sources you rely on inline as [1], [2], ... with a numbered list of their URLs at
the end. If the results don't answer the question, say so rather than guessing."""

# Without this, a model that still wants to search narrates "let me search more" in a loop
# instead of answering, and the judges end up scoring that rather than the search engine.
FINAL_TURN = (
    "You have used all your searches. Now write your final answer to the user, based on the search "
    "results above. Don't describe further searching; if something wasn't found, say so briefly."
)

JUDGE_PROMPT = f"""\
You are an expert evaluator of search-augmented AI assistants. Today's date is {TODAY}.
In each comparison below, two assistants answered the same user prompt. Both used the same language
model; they differ only in the web search engine they could call, so differences mostly reflect the
quality of what each one found.

For each comparison, judge which response serves the user better, considering in order:
  1. Correctness and grounding: are the claims accurate and supported by cited sources? Penalise
     fabrication, outdated information presented as current, and confident claims without support.
  2. Helpfulness: does it actually answer what was asked, with the specifics the user wanted?
  3. Completeness: does it cover the important aspects?
  4. Source quality: are the cited sources credible, relevant and primary where possible?
  5. Honesty: if information wasn't found, does it say so rather than guess?
Ignore length and style except where they affect usefulness. Don't guess which search engine was
used. If they are about equally good, say tie.

OUTPUT: one JSON object per comparison, one per line, nothing else, keys in this order:
  "id":       the comparison id
  "better":   "A", "B" or "tie"
  "strength": "slight", "clear" or "strong" ("none" for a tie)
  "reason":   one of "correctness", "helpfulness", "completeness", "sources", "honesty", "none"
  "note":     at most 25 words saying why

COMPARISONS:
"""

session = requests.Session()
write_lock = threading.Lock()
brave_lock = threading.Lock()
brave_last_request = [0.0]


def sample(parquet: Path, work: Path) -> None:
    import duckdb

    rows = (
        duckdb.connect()
        .sql(
            f"""
            select messages_a[1].content, primary_intent from '{parquet}'
            where languages = ['English'] and primary_intent not in {EXCLUDED_INTENTS}
            """
        )
        .fetchall()
    )
    seen = set()
    pool = []
    for content, intent in rows:
        query = " ".join(content.split())
        is_search_shaped = 2 <= len(query.split()) <= 20 and "http" not in query
        if is_search_shaped and query.lower() not in seen:
            seen.add(query.lower())
            pool.append({"query": query, "intent": intent})
    random.Random(SAMPLE_SEED).shuffle(pool)
    work.mkdir(parents=True, exist_ok=True)
    (work / "queries.json").write_text(json.dumps(pool[:NUM_QUERIES], indent=1))
    print(f"{NUM_QUERIES} of {len(pool)} eligible prompts -> {work / 'queries.json'}")


def llm(model: str, messages: list[dict], **kwargs) -> dict:
    for attempt in range(6):
        try:
            response = session.post(
                LLM_URL,
                headers={"Authorization": f"Bearer {os.environ['REGOLO_API_KEY']}"},
                json={"model": model, "messages": messages, **kwargs},
                timeout=600,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]
        except requests.RequestException as error:
            print("LLM retry", attempt, repr(error)[:200], flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("LLM request failed")


def search_combined(query: str) -> list[dict]:
    response = session.get(
        COMBINED_URL, params={"q": query}, headers={"X-API-Key": os.environ["MWMBL_SEARCH_API_KEY"]}, timeout=60
    )
    response.raise_for_status()
    results = response.json()["results"][:NUM_RESULTS]
    return [{"title": r["title"], "url": r["url"], "snippet": r["content"]} for r in results]


def search_brave(query: str) -> list[dict]:
    with brave_lock:
        wait = brave_last_request[0] + BRAVE_MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        brave_last_request[0] = time.monotonic()
    response = session.get(
        BRAVE_URL,
        params={"q": query, "count": NUM_RESULTS},
        headers={"Accept": "application/json", "X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"]},
        timeout=30,
    )
    response.raise_for_status()
    results = response.json().get("web", {}).get("results", [])[:NUM_RESULTS]
    return [{"title": r["title"], "url": r["url"], "snippet": TAG.sub("", r.get("description", ""))} for r in results]


SEARCH = {"combined": search_combined, "brave": search_brave}


def answer_query(query: str, arm: str, model: str) -> dict:
    messages = [{"role": "system", "content": ANSWER_SYSTEM}, {"role": "user", "content": query}]
    searches = []
    while True:
        can_search = len(searches) < MAX_SEARCHES
        if not can_search:
            messages.append({"role": "user", "content": FINAL_TURN})
        tool_options = {"tools": TOOLS, "tool_choice": "auto"} if can_search else {}
        message = llm(model, messages, temperature=0.3, max_tokens=8000, **tool_options)
        calls = message.get("tool_calls") or []
        if not calls or not can_search:
            return {"query": query, "arm": arm, "answer": message.get("content") or "", "searches": searches}
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        for call in calls:
            search_query = json.loads(call["function"]["arguments"])["query"]
            if len(searches) < MAX_SEARCHES:
                results = SEARCH[arm](search_query)
                searches.append({"query": search_query, "urls": [r["url"] for r in results]})
                payload = {"results": results, "searches_remaining": MAX_SEARCHES - len(searches)}
                content = json.dumps(payload, ensure_ascii=False)
            else:
                content = "Search limit reached; answer with what you have."
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open()] if path.exists() else []


def answer(work: Path, arm: str, model: str) -> None:
    queries = json.loads((work / "queries.json").read_text())
    out = work / f"answers_{arm}.jsonl"
    done = {record["query"] for record in load(out)}

    def run(query: dict) -> None:
        if query["query"] in done:
            return
        record = answer_query(query["query"], arm, model)
        with write_lock, out.open("a") as f:
            f.write(json.dumps(dict(record, intent=query["intent"], model=model), ensure_ascii=False) + "\n")
        print(arm, len(record["searches"]), "searches,", len(record["answer"]), "chars |", query["query"][:50])

    with ThreadPoolExecutor(CONCURRENCY) as pool:
        list(pool.map(run, queries))


def comparison_block(judgment_id: int, query: str, a_answer: str, b_answer: str) -> str:
    return (
        f"\n=== COMPARISON {judgment_id}\nUSER PROMPT: {query}\n"
        f"\n--- RESPONSE A\n{a_answer or '(empty response)'}\n"
        f"\n--- RESPONSE B\n{b_answer or '(empty response)'}\n"
    )


def batches(work: Path) -> None:
    queries = json.loads((work / "queries.json").read_text())
    answers = {arm: {r["query"]: r["answer"] for r in load(work / f"answers_{arm}.jsonl")} for arm in ARMS}
    answered = [q for q in queries if all(q["query"] in answers[arm] for arm in ARMS)]

    # The first orientation is random; each half holds one orientation of every comparison.
    rng = random.Random(1)
    halves: list[list[tuple[dict, str, str]]] = [[], []]
    for query in answered:
        first, second = rng.sample(ARMS, 2)
        halves[0].append((query, first, second))
        halves[1].append((query, second, first))

    judge_dir = work / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    num_batches = 0
    for half in halves:
        rng.shuffle(half)
        for start in range(0, len(half), COMPARISONS_PER_BATCH):
            blocks = []
            for query, a, b in half[start : start + COMPARISONS_PER_BATCH]:
                judgment_id = len(manifest) + 1
                text = query["query"]
                blocks.append(comparison_block(judgment_id, text, answers[a][text], answers[b][text]))
                manifest[judgment_id] = {"query": text, "intent": query["intent"], "a": a, "b": b}
            (judge_dir / f"batch_{num_batches:02d}.txt").write_text(JUDGE_PROMPT + "".join(blocks))
            num_batches += 1
    (judge_dir / "manifest.json").write_text(json.dumps(manifest))
    print(f"{len(manifest)} judgments in {num_batches} batches -> {judge_dir}")


def consolidate(work: Path) -> None:
    judge_dir = work / "judge"
    manifest = json.loads((judge_dir / "manifest.json").read_text())
    verdicts = {}
    for path in sorted(judge_dir.glob("out_*.txt")):
        for line in path.read_text().splitlines():
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            verdict = json.loads(line)
            judgment_id = str(verdict["id"])
            assert verdict["better"] in ("A", "B", "tie"), line
            assert verdict["better"] == "tie" or verdict["strength"] in STRENGTHS, line
            assert judgment_id in manifest and judgment_id not in verdicts, f"{path.name}: bad or repeated id"
            verdicts[judgment_id] = dict(verdict, judge=path.stem.removeprefix("out_"))
    missing = sorted(set(manifest) - set(verdicts), key=int)
    assert not missing, f"{len(missing)} ids unjudged, first {missing[:10]}"
    judgments_path = work / "judgments.jsonl"
    with judgments_path.open("w") as f:
        for judgment_id, judgment in sorted(manifest.items(), key=lambda item: int(item[0])):
            f.write(json.dumps(dict(judgment, id=int(judgment_id), verdict=verdicts[judgment_id])) + "\n")
    print(f"{len(verdicts)} judgments -> {judgments_path}")


def preference_for_combined(record: dict) -> int:
    """The verdict as -3..3 in favour of Combined Search."""
    verdict = record["verdict"]
    if verdict["better"] == "tie":
        return 0
    chosen = record["a"] if verdict["better"] == "A" else record["b"]
    size = STRENGTHS[verdict["strength"]]
    return size if chosen == "combined" else -size


def bootstrap_ci(values: np.ndarray) -> tuple[float, float]:
    rng = np.random.default_rng(0)
    means = [values[rng.integers(0, len(values), len(values))].mean() for _ in range(BOOTSTRAP_SAMPLES)]
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def host(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]


def report(work: Path) -> None:
    records = load(work / "judgments.jsonl")
    answers = {arm: {r["query"]: r for r in load(work / f"answers_{arm}.jsonl")} for arm in ARMS}
    by_query: dict[str, list[dict]] = {}
    for record in records:
        by_query.setdefault(record["query"], []).append(record)
    queries = list(by_query)
    preferences = np.array([[preference_for_combined(r) for r in by_query[q]] for q in queries])
    per_query = preferences.mean(axis=1)
    low, high = bootstrap_ci(per_query)
    decided = [r["verdict"]["better"] for r in records if r["verdict"]["better"] != "tie"]
    signs = np.sign(preferences)

    print(f"- {len(queries)} queries, {len(records)} judgments")
    print(f"- mean preference for Combined (-3..3): {per_query.mean():+.2f} [{low:+.2f}, {high:+.2f}]")
    print(
        f"- Combined preferred {np.mean(per_query > 0):.0%}, Brave {np.mean(per_query < 0):.0%}, "
        f"tie {np.mean(per_query == 0):.0%}"
    )
    print(f"- both orders agree on the direction: {np.mean(signs[:, 0] == signs[:, 1]):.0%}")
    print(f"- A chosen in {decided.count('A') / len(decided):.0%} of non-ties")

    # Longer answers tend to win on completeness; the intercept is the preference at equal length.
    length_ratio = np.array(
        [np.log(len(answers["combined"][q]["answer"]) / len(answers["brave"][q]["answer"])) for q in queries]
    )
    intercept, slope = np.linalg.lstsq(np.c_[np.ones_like(length_ratio), length_ratio], per_query, rcond=None)[0]
    print(f"- length-adjusted preference: {intercept:+.2f} (slope {slope:+.2f} per log length ratio)")

    winners = Counter()
    for record in records:
        preference = preference_for_combined(record)
        winner = "combined" if preference > 0 else "brave" if preference < 0 else "tie"
        winners[(winner, record["verdict"]["reason"])] += 1
    print("- reasons: " + ", ".join(f"{winner}/{reason} {n}" for (winner, reason), n in winners.most_common()))

    by_intent: dict[str, list[float]] = {}
    for query, preference in zip(queries, per_query):
        by_intent.setdefault(by_query[query][0]["intent"], []).append(preference)
    for intent, values in sorted(by_intent.items(), key=lambda item: -len(item[1])):
        print(f"  {intent:16} n={len(values):3} preference {np.mean(values):+.2f}")

    for arm in ARMS:
        arm_answers = list(answers[arm].values())
        hosts = Counter(host(url) for a in arm_answers for search in a["searches"] for url in search["urls"])
        print(
            f"- {arm}: {np.mean([len(a['searches']) for a in arm_answers]):.2f} searches, "
            f"{sum(len(a['searches']) >= MAX_SEARCHES for a in arm_answers)} at the cap, "
            f"median {np.median([len(a['answer']) for a in arm_answers]):.0f} chars; top hosts "
            + ", ".join(f"{h} {n}" for h, n in hosts.most_common(6))
        )


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sample_parser = commands.add_parser("sample")
    sample_parser.add_argument("parquet", type=Path)
    sample_parser.add_argument("work", type=Path)
    answer_parser = commands.add_parser("answer")
    answer_parser.add_argument("work", type=Path)
    answer_parser.add_argument("arm", choices=ARMS)
    answer_parser.add_argument("model")
    for name in ("batches", "consolidate", "report"):
        commands.add_parser(name).add_argument("work", type=Path)
    args = parser.parse_args()

    if args.command == "sample":
        sample(args.parquet, args.work)
    elif args.command == "answer":
        answer(args.work, args.arm, args.model)
    elif args.command == "batches":
        batches(args.work)
    elif args.command == "consolidate":
        consolidate(args.work)
    else:
        report(args.work)


if __name__ == "__main__":
    main()
