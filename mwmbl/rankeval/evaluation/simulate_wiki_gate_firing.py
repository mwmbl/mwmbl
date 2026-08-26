"""How often would a wiki-index gate fire, and on the right query?

evaluate_wiki_index answers what a gate *costs*, and pays for the answer: every query is
a full pass through the remote production index and the LTR model, so a run is limited to
a sample of a few hundred to a couple of thousand queries. This asks the cheaper question -
how often does a gate fire, and is the firing a genuine repeat or a different query
riding on stored documents - over every query whose Wikipedia response is already on disk,
in about a minute and with no network.

That works because the gate only counts documents a previous call *stored*
(``wiki_documents_by_term(stored_only=True)`` filters to WIKI_STATES), so the crawled half
of production contributes nothing to it and a fresh local index holding exactly what this
run stored is a faithful simulation of the gate's input. NDCG is not: what a firing costs
depends on the whole candidate pool, so read this alongside evaluate_wiki_index, never
instead of it.

The split that matters is first-seen against repeat. A repeat firing serves the very URLs
the API returned for this query last time; a first-seen firing serves some other query's
documents, and every gate this harness has measured paid around -0.19 NDCG for those.

```bash
DATABASE_URL="postgres://daoud@" DJANGO_SETTINGS_MODULE=mwmbl.settings_dev \
    uv run python -m mwmbl.rankeval.evaluation.simulate_wiki_gate_firing
```
"""
import json
import os
import re
import sys
from argparse import ArgumentParser
from collections import Counter
from pathlib import Path

import django
import joblib
import numpy as np

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mwmbl.settings_dev")
django.setup()

from mwmbl.indexer.index_batches import index_results_against_query  # noqa: E402
from mwmbl.rankeval.paths import DATA_DIR  # noqa: E402
from mwmbl.tinysearchengine.indexer import Document, DocumentState, TinyIndex  # noqa: E402
from mwmbl.tinysearchengine.wiki_index_cache import (  # noqa: E402
    DENOMINATORS, VALUE_GATES, have_enough_wiki_results, query_terms,
)
from mwmbl.tokenizer import tokenize  # noqa: E402


# joblib keys a cached function's directory on the defining module and file path, and
# evaluate_wiki_index is run as __main__, so the name depends on where the checkout lives.
# Glob for it rather than reconstructing it.
WIKI_RESPONSE_CACHE_ROOT = DATA_DIR / "wiki-index-eval-cache" / "joblib"


def response_cache_dir() -> Path | None:
    """evaluate_wiki_index._fetch_wiki's cache - responses that harness already paid for."""
    return next(WIKI_RESPONSE_CACHE_ROOT.glob("*evaluate_wiki_index/_fetch_wiki"), None)

# One arm is a (gate, score_terms) pair. The two are not independent: bigram_coverage and
# term_coverage are the same condition under "all" (see query_bigrams), and term_coverage
# cannot reach full coverage at all under "specific", so only the 2x2 says anything.
ARMS = [
    ("terms/all           (today's default)", "term_coverage", "all"),
    ("terms/specific      (the gate dies)", "term_coverage", "specific"),
    ("bigrams/all         (denominator alone)", "bigram_coverage", "all"),
    ("bigrams/specific    (both halves)", "bigram_coverage", "specific"),
]


def cached_responses() -> dict[str, list[dict]]:
    """Every query whose Wikipedia response evaluate_wiki_index has already fetched."""
    cache = response_cache_dir()
    if cache is None:
        raise SystemExit(
            f"No cached Wikipedia responses under {WIKI_RESPONSE_CACHE_ROOT}.\n"
            "Run evaluate_wiki_index first - this reads the responses it cached.")
    responses = {}
    for call in sorted(cache.iterdir()):
        metadata, output = call / "metadata.json", call / "output.pkl"
        if not (metadata.exists() and output.exists()):
            continue
        query = json.loads(metadata.read_text())["input_args"]["query"]
        responses[query[1:-1]] = joblib.load(output)  # the arg is repr()d
    return responses


def to_documents(rows: list[dict]) -> list[Document]:
    return [Document(r["title"], r["url"], r["extract"], r["score"], r["term"],
                     state=DocumentState.FROM_WIKI) for r in rows]


def retrieve(index: TinyIndex, query: str) -> list[Document]:
    return [document for term in query_terms(query) for document in index.retrieve(term)]


def zipf_stream(queries: list[str], length: int, seed: int) -> list[str]:
    """The same repeat-weighted replay evaluate_wiki_index.run_zipf uses."""
    rng = np.random.default_rng(seed)
    weights = 1.0 / np.arange(1, len(queries) + 1)
    return list(rng.choice(np.array(queries, dtype=object), length, replace=True,
                           p=weights / weights.sum()))


def run_arm(name: str, gate: str, score_terms: str, stream: list[str],
            responses: dict[str, list[dict]], threshold: float, pages: int,
            scratch: Path) -> tuple[Counter, list[tuple[int, str]]]:
    path = scratch / f"overlay-{re.sub('[^a-z0-9]+', '-', name.lower()).strip('-')}.tinysearch"
    path.unlink(missing_ok=True)
    with TinyIndex.create(Document, str(path), num_pages=pages, page_size=4096):
        pass

    counts, cross_query, seen = Counter(), [], set()
    try:
        with TinyIndex(Document, str(path), 'r') as index:
            for query in stream:
                fired = have_enough_wiki_results(query, retrieve(index, query),
                                                 rank=None, gate=gate, threshold=threshold)
                repeat = query in seen
                seen.add(query)
                counts["fired" if fired else "called", "repeat" if repeat else "first"] += 1
                if fired and not repeat:
                    cross_query.append((len(tokenize(query)), query))
                if not fired:
                    # A miss is what writes: same call, same write path, same settings.
                    index_results_against_query(
                        to_documents(responses[query]), query, str(path),
                        max_term_tokens=2, state=DocumentState.FROM_WIKI,
                        score_terms=score_terms)
    finally:
        path.unlink(missing_ok=True)
    return counts, cross_query


def run():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--stream", type=int, default=3000,
                        help="Positions in the repeat-weighted replay.")
    parser.add_argument("--threshold", type=float, default=1.0,
                        help="Coverage needed to skip the call. Below 1.0 a gate fires on "
                             "a partially covered query, which is the case that costs.")
    parser.add_argument("--pages", type=int, default=65536,
                        help="Pages in the simulated index. Enough that page-full "
                             "truncation does not confound the count.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scratch", default=f"/tmp/mwmbl-wiki-gate-sim-{os.getpid()}")
    args = parser.parse_args()

    responses = cached_responses()
    queries = sorted(responses)
    stream = zipf_stream(queries, args.stream, args.seed)
    repeats = sum(1 for i, query in enumerate(stream) if query in set(stream[:i]))
    print(f"{len(queries)} cached queries; {len(stream)} positions, {repeats} repeats; "
          f"threshold {args.threshold:g}\n")

    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    print(f"{'denominator / scores':40} {'fired':>6} {'repeat':>7} {'first-seen':>11} "
          f"{'repeats caught':>15}")
    for name, gate, score_terms in ARMS:
        assert gate in VALUE_GATES and VALUE_GATES[gate][2] in DENOMINATORS
        counts, cross_query = run_arm(name, gate, score_terms, stream, responses,
                                      args.threshold, args.pages, scratch)
        on_repeat, on_first = counts["fired", "repeat"], counts["fired", "first"]
        print(f"{name:40} {on_repeat + on_first:6d} {on_repeat:7d} {on_first:11d} "
              f"{on_repeat / repeats:14.1%}", flush=True)
        if cross_query:
            by_length = sorted(Counter(n for n, _ in cross_query).items())
            print(f"{'':40} first-seen firings by query length: {by_length}")
            for length, query in sorted(cross_query)[:5]:
                print(f"{'':44} {length}-token {query!r}")
    scratch.rmdir()


if __name__ == "__main__":
    sys.exit(run())
