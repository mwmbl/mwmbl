"""Wikidata entities and their links for the en-gb rows: raw material for a Wikidata fill.

For each query, the top wbsearchentities hits, each with its official websites (P856), its
English Wikipedia sitelink and every external-ID claim whose property has a formatter URL
(P1630), e.g. IMDb, Instagram, MusicBrainz. Nothing is chosen here; the arms do that.
"""

import json
import os
import time

import httpx

API = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "mwmbl-eval/1.0 (https://mwmbl.org; hello@mwmbl.org)"}
P = "devdata/combined_providers_eval/engb/"
NUM_ENTITIES = 3
client = httpx.Client(headers=HEADERS, timeout=20)


def get(params: dict) -> dict:
    # Wikimedia rate-limits parallel bots with 429s: go serially, and back off when told to.
    for attempt in range(6):
        r = client.get(API, params={"format": "json", **params})
        if r.status_code == 429:
            time.sleep(float(r.headers.get("retry-after", 2**attempt * 5)))
            continue
        r.raise_for_status()
        time.sleep(0.2)
        return r.json()
    r.raise_for_status()


def search(query: str) -> list[dict]:
    hits = get(
        {
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "uselang": "en",
            "limit": NUM_ENTITIES,
            "type": "item",
        }
    )
    return hits.get("search", [])


def entities(ids: list[str]) -> dict:
    if not ids:
        return {}
    return get(
        {
            "action": "wbgetentities",
            "ids": "|".join(ids),
            "props": "claims|labels|descriptions|sitelinks/urls",
            "languages": "en",
            "sitefilter": "enwiki",
        }
    )["entities"]


def claim_values(claims: list[dict]) -> list[tuple[str, str]]:
    out = []
    for c in claims:
        if c.get("rank") == "deprecated":
            continue
        try:
            out.append((c["mainsnak"]["datavalue"]["value"], c.get("rank")))
        except (KeyError, TypeError):
            pass
    return out


def fetch(query: str) -> list[dict]:
    hits = search(query)
    ents = entities([h["id"] for h in hits])
    result = []
    for h in hits:
        e = ents.get(h["id"], {})
        claims = e.get("claims", {})
        result.append(
            {
                "id": h["id"],
                "label": (e.get("labels", {}).get("en") or {}).get("value", h.get("label", "")),
                "description": (e.get("descriptions", {}).get("en") or {}).get("value", ""),
                "match": h.get("match", {}),
                "enwiki": (e.get("sitelinks", {}).get("enwiki") or {}).get("url"),
                "official": [v for v, _ in claim_values(claims.get("P856", []))],
                "external_ids": {
                    pid: [v for v, _ in claim_values(cs) if isinstance(v, str)]
                    for pid, cs in claims.items()
                    if cs and cs[0].get("mainsnak", {}).get("datatype") == "external-id"
                },
            }
        )
    return result


rows = json.load(open(P + "rows-0.05.json"))
queries = [r["query"] for r in rows]
checkpoint = P + "wikidata_raw.partial.json"
pools = json.load(open(checkpoint)) if os.path.exists(checkpoint) else {}
for i, q in enumerate(queries):
    if q not in pools:
        pools[q] = fetch(q)
        if i % 20 == 0:
            json.dump(pools, open(checkpoint, "w"))
            print(i, flush=True)
json.dump(pools, open(checkpoint, "w"))

# Formatter URLs for every external-ID property seen.
pids = sorted({pid for ents in pools.values() for e in ents for pid in e["external_ids"]})
formatters, labels = {}, {}
for i in range(0, len(pids), 50):
    props = get(
        {"action": "wbgetentities", "ids": "|".join(pids[i : i + 50]), "props": "claims|labels", "languages": "en"}
    )["entities"]
    for pid, p in props.items():
        fs = claim_values(p.get("claims", {}).get("P1630", []))
        preferred = [v for v, rank in fs if rank == "preferred"] or [v for v, _ in fs]
        if preferred:
            formatters[pid] = preferred[0]
        labels[pid] = (p.get("labels", {}).get("en") or {}).get("value", pid)

json.dump({"entities": pools, "formatters": formatters, "property_labels": labels}, open(P + "wikidata_raw.json", "w"))
n = [len(v) for v in pools.values()]
print(
    "queries",
    len(pools),
    "with an entity",
    sum(x > 0 for x in n),
    "properties",
    len(pids),
    "with formatter",
    len(formatters),
)
print("top entity has official site:", sum(bool(v and v[0]["official"]) for v in pools.values()))
