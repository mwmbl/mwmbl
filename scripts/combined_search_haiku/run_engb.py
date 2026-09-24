"""The combined-providers eval with both Staan and Brave asked for UK results.

Staan's market comes from settings_engb. Brave gets country=GB here, through a joblib cache
of its own: the eval's brave_results is cached by query alone, so it would hand back the
country=US answers from the earlier run.
"""

import json
import time
from pathlib import Path

import requests

from mwmbl.rankeval.evaluation import compare_combined_providers as ccp


@ccp.memory.cache
def brave_results_gb(query: str) -> list[dict]:
    time.sleep(ccp.BRAVE_MIN_INTERVAL_SECONDS)
    response = requests.get(
        ccp.BRAVE_SEARCH_URL,
        params={"q": query, "count": ccp.NUM_RESULTS_FOR_EVAL, "country": "GB", "search_lang": "en"},
        headers={"Accept": "application/json", "X-Subscription-Token": ccp.BRAVE_SEARCH_API_KEY},
        timeout=10,
    )
    response.raise_for_status()
    web_results = response.json().get("web", {}).get("results", [])
    return [
        {"url": result["url"], "title": result["title"], "extract": result.get("description", "")}
        for result in web_results[: ccp.NUM_RESULTS_FOR_EVAL]
    ]


def brave_results_cached_only(query: str) -> list[dict]:
    # The Brave quota ran out (402) three queries from the end: serve what is cached and
    # return nothing for the rest, which the analysis drops, rather than cache a failure.
    if brave_results_gb.check_call_in_cache(query):
        return brave_results_gb(query)
    return []


ccp.brave_results = brave_results_cached_only

# Keep each arm's title and extract, so the Haiku judge can grade the pool without a rebuild.
pool_text: dict[str, dict[str, list[str]]] = {}
original_build_arms = ccp.build_arms


def build_arms_recording(include_brave):
    def recording(name, arm):
        def wrapped(query, staan_docs, wiki_docs):
            results = arm(query, staan_docs, wiki_docs)
            for result in results:
                pool_text.setdefault(query, {}).setdefault(result["url"], [result["title"], result["extract"], name])
            return results

        return wrapped

    return {name: recording(name, arm) for name, arm in original_build_arms(include_brave).items()}


ccp.build_arms = build_arms_recording
ccp.OUTPUT_DIR = Path("devdata/combined_providers_eval/engb")
ccp.run()
(ccp.OUTPUT_DIR / "pool_text.json").write_text(json.dumps(pool_text))
