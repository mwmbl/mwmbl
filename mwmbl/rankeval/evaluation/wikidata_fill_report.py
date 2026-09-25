"""Can Wikidata fill the slots Staan leaves empty? Offline, from committed data.

Every table in ``mwmbl/rankeval/combined-search-wikidata-fill.md`` comes from this script. Like
``haiku_arms_report``, it needs no network, no Django and no API keys. It reads:

- ``engb/rows-0.05.json`` and ``rows-0.05.json``: the eval rows of the two runs.
- ``engb/staan_page2_sample.json``: Staan pages 1 and 2 for 20 queries whose page 1 was short.
- ``engb/wikidata_raw.json``: the top ``wbsearchentities`` hits for every en-gb query, with
  their official websites, English Wikipedia sitelinks and external IDs.
- ``haiku/relevance_engb_wikidata.jsonl`` and ``haiku/pass3_engb_wikidata.jsonl``: Haiku's
  grades for the Wikidata URLs no earlier judgment covered, plus re-grades of already-graded
  "anchor" URLs from the same queries, to check the new run against the old.

Usage::

    .venv/bin/python -m mwmbl.rankeval.evaluation.wikidata_fill_report
"""

import json
import re
from collections import Counter
from urllib.parse import quote, urlsplit

import numpy as np

from mwmbl.rankeval.evaluation.haiku_arms_report import (
    DISCOUNTS,
    EVAL_DIR,
    NUM_RESULTS,
    bootstrap,
    load_json,
    load_judgments,
    ndcg,
    staan_first,
)

# User-facing external IDs, best-first after the official website: (property, site, URL
# template). Chosen from the properties the eval's matched entities actually carry, leaving
# out authority files (VIAF, LoC, GND...), search-engine IDs (Freebase, Google KG) and
# non-English sites. IMDb's formatter URL is a toolforge redirect, so it has its own
# templates, by ID prefix.
LINK_PROPERTIES = [
    ("P345", "IMDb", None),
    ("P1417", "Britannica", "https://www.britannica.com/$1"),
    ("P3106", "The Guardian", "https://www.theguardian.com/$1"),
    ("P6200", "BBC News", "https://www.bbc.co.uk/news/topics/$1"),
    ("P1258", "Rotten Tomatoes", "https://www.rottentomatoes.com/$1"),
    ("P1712", "Metacritic", "https://www.metacritic.com/$1"),
    ("P4947", "TMDB", "https://www.themoviedb.org/movie/$1"),
    ("P4983", "TMDB", "https://www.themoviedb.org/tv/$1"),
    ("P4985", "TMDB", "https://www.themoviedb.org/person/$1"),
    ("P434", "MusicBrainz", "https://musicbrainz.org/artist/$1"),
    ("P1953", "Discogs", "https://www.discogs.com/artist/$1"),
    ("P1902", "Spotify", "https://open.spotify.com/artist/$1"),
    ("P2963", "Goodreads", "https://www.goodreads.com/author/show/$1"),
    ("P2446", "Transfermarkt", "https://www.transfermarkt.com/-/profil/spieler/$1"),
    ("P7223", "Transfermarkt", "https://www.transfermarkt.com/-/startseite/verein/$1"),
    ("P3134", "Tripadvisor", "https://www.tripadvisor.co.uk/$1"),
    ("P402", "OpenStreetMap", "https://www.openstreetmap.org/relation/$1"),
    ("P2003", "Instagram", "https://www.instagram.com/$1/"),
    ("P2002", "X", "https://x.com/$1"),
    ("P2013", "Facebook", "https://www.facebook.com/$1"),
    ("P2397", "YouTube", "https://www.youtube.com/channel/$1"),
]
IMDB_TEMPLATES = {
    "tt": "https://www.imdb.com/title/$1/",
    "nm": "https://www.imdb.com/name/$1/",
    "co": "https://www.imdb.com/search/title/?companies=$1",
    "ch": "https://www.imdb.com/character/$1/",
}
BASELINE = "staan-first + fill"
MINILM_FILL = "staan-first, fill MiniLM(LTR top 30 + Wikipedia)"
ALL_LINKS = "staan, Wikidata links, fill"


def normalise_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def canonical(url: str) -> str:
    return host(url) + urlsplit(url).path.rstrip("/").lower()


def matched_entity(query: str, entities: list[dict]) -> dict | None:
    """The first hit whose matched label or alias is the query itself, not just a prefix of it.

    wbsearchentities matches prefixes, so without this "arnold press" is Arnold Pressburger.
    """
    for entity in entities:
        if normalise_text(entity["match"].get("text", "")) == normalise_text(query):
            return entity
    return None


def entity_links(entity: dict) -> list[dict]:
    """The entity's candidate results, best-first: official website, then LINK_PROPERTIES.

    The title and extract are what a result built from Wikidata alone would show.
    """
    label, description = entity["label"], entity["description"]
    about = f"{label}: {description}" if description else label
    links = [
        {"url": url, "title": label, "extract": f"Official website. {about}", "kind": "official"}
        for url in entity["official"][:1]
    ]
    for pid, site, template in LINK_PROPERTIES:
        for value in entity["external_ids"].get(pid, [])[:1]:
            url_template = IMDB_TEMPLATES.get(value[:2]) if pid == "P345" else template
            if url_template:
                url = url_template.replace("$1", quote(value, safe="/:@-_.~"))
                links.append({"url": url, "title": f"{label} - {site}", "extract": about, "kind": site})
    return links


def wikidata_candidates() -> dict[str, list[dict]]:
    raw = load_json("engb/wikidata_raw.json")["entities"]
    candidates = {}
    for query, entities in raw.items():
        entity = matched_entity(query, entities)
        candidates[query] = entity_links(entity) if entity else []
    return candidates


def fill(staan: list[str], links: list[dict], rest: list[str], max_links: int = NUM_RESULTS) -> list[str]:
    """Staan in its own order, then up to `max_links` new Wikidata links, then `rest`, to ten.

    A link is new when Staan doesn't have its URL (ignoring the query string, case and a
    trailing slash) and, for an official website, doesn't have its host either: Staan's page
    on the official site is as good as its home page. `rest` is deduplicated exactly as
    staan_first does it, so with no links this is staan_first.
    """
    seen = {canonical(url) for url in staan}
    hosts = {host(url) for url in staan}
    added = []
    for link in links:
        if len(added) >= max_links:
            break
        if canonical(link["url"]) in seen or (link["kind"] == "official" and host(link["url"]) in hosts):
            continue
        added.append(link["url"])
        seen.add(canonical(link["url"]))
    return staan_first(staan + added, rest)


def ltr_and_wikipedia_by_minilm() -> dict[str, list[str]]:
    """The LTR's top 30 plus the Wikipedia candidates, sorted by MiniLM, as in haiku_arms_report."""
    deep, wiki = load_json("engb/combined_top30.json"), load_json("engb/wiki_pool.json")
    result = {}
    for query, docs in deep.items():
        docs = docs + wiki.get(query, [])
        minilm = {doc["url"]: doc["minilm"] for doc in docs}
        result[query] = sorted(dict.fromkeys(doc["url"] for doc in docs), key=lambda url: -minilm.get(url, 0))
    return result


def wikidata_arms(row: dict, links: list[dict], ltr_and_wiki: list[str]) -> dict[str, list[str]]:
    staan, combined = row["lists"]["staan"][:NUM_RESULTS], row["lists"]["combined"][:NUM_RESULTS]
    official = [link for link in links if link["kind"] == "official"]
    arms = {
        BASELINE: fill(staan, [], combined),
        "staan, Wikidata official site, fill": fill(staan, official, combined),
        "staan, 1 Wikidata link, fill": fill(staan, links, combined, 1),
        "staan, 2 Wikidata links, fill": fill(staan, links, combined, 2),
        ALL_LINKS: fill(staan, links, combined),
        MINILM_FILL: fill(staan, [], ltr_and_wiki),
        "staan, 2 Wikidata links, fill MiniLM(LTR top 30 + Wikipedia)": fill(staan, links, ltr_and_wiki, 2),
        "brave": row["lists"]["brave"][:NUM_RESULTS],
    }
    return arms


def merged_judgments(original: str, wikidata: str) -> tuple[dict, list[tuple[dict, dict]]]:
    """The original judgments plus the new URLs' grades; anchors keep their original grade.

    Also returns (original, re-grade) pairs for the anchors.
    """
    judgments = load_judgments(original)
    anchors = []
    for line in (EVAL_DIR / wikidata).read_text().splitlines():
        record = json.loads(line)
        if record["anchor"]:
            anchors.append((judgments[record["query"]][record["url"]], record))
        else:
            judgments.setdefault(record["query"], {})[record["url"]] = record
    return judgments, anchors


def report_short_staan() -> None:
    print("\n# How often Staan returns fewer than ten, and what filling those slots is worth\n")
    print("Mean Haiku relevance NDCG@10 gain of staan-first + fill over Staan alone, by Staan's list length.\n")
    runs = [
        ("en-us", "rows-0.05.json", "haiku/relevance_enus.jsonl"),
        ("en-gb", "engb/rows-0.05.json", "haiku/relevance_engb.jsonl"),
    ]
    for name, rows_path, judgments_path in runs:
        judgments = load_judgments(judgments_path)
        gains: dict[int, list[float]] = {}
        fill_grades, tail_grades = [], []
        for row in load_json(rows_path):
            graded = judgments.get(row["query"])
            if not graded:
                continue
            staan = row["lists"]["staan"][:NUM_RESULTS]
            filled = staan_first(staan, row["lists"]["combined"][:NUM_RESULTS])
            all_gains = [2 ** g["relevance"] - 1 for g in graded.values()]

            def score(urls: list[str]) -> float:
                return ndcg([2 ** graded[u]["relevance"] - 1 if u in graded else 0 for u in urls], all_gains)

            gains.setdefault(len(staan), []).append(score(filled) - score(staan))
            fill_grades += [graded[u]["relevance"] for u in filled[len(staan) :] if u in graded]
            if len(staan) == NUM_RESULTS:
                tail_grades += [graded[u]["relevance"] for u in staan[7:] if u in graded]
        total = sum(len(v) for v in gains.values())
        print(f"## {name} ({total} queries)\n")
        print("| Staan results | Queries | Mean gain | Share of the overall gain |\n|---|---|---|---|")
        overall = sum(sum(v) for v in gains.values())
        for length in sorted(gains, reverse=True):
            values = gains[length]
            share = sum(values) / overall if overall else 0
            print(f"| {length} | {len(values)} | {np.mean(values):+.3f} | {share:.0%} |")
        print(f"| all | {total} | {overall / total:+.3f} | |\n")
        print(
            f"Mean relevance (0-3) of the fill: {np.mean(fill_grades):.2f}. "
            f"Of Staan's own 8th-10th results, when it returns ten: {np.mean(tail_grades):.2f}.\n"
        )
    print(
        f"Discounts at positions 8-10: {', '.join(f'{d:.3f}' for d in DISCOUNTS[7:])}, "
        f"{DISCOUNTS[7:].sum() / DISCOUNTS.sum():.0%} of an all-perfect top ten's DCG."
    )


def report_page2() -> None:
    sample = load_json("engb/staan_page2_sample.json")
    print(f"\n# Staan page 2, for {len(sample)} en-gb queries whose page 1 came back short\n")
    print("| Query | Stored | Page 1 now | Page 2 | Page 2 not on page 1 |\n|---|---|---|---|---|")
    for r in sample:
        print(f"| {r['query']} | {r['stored']} | {r['n0']} | {r['n10']} | {r['p2_new']} |")
    unusable = sum(r["n0"] - r["usable0"] + r["n10"] - r["usable10"] for r in sample)
    print(
        f"\nPage 1 averages {np.mean([r['n0'] for r in sample]):.1f} results "
        f"({sum(r['n0'] < NUM_RESULTS for r in sample)} of {len(sample)} still short); page 2 averages "
        f"{np.mean([r['n10'] for r in sample]):.1f}, {np.mean([r['p2_new'] for r in sample]):.1f} of them new. "
        f"Results without a URL or title: {unusable}."
    )


def report_coverage(rows: list[dict], candidates: dict[str, list[dict]]) -> None:
    raw = load_json("engb/wikidata_raw.json")["entities"]
    matched = [r for r in rows if matched_entity(r["query"], raw[r["query"]])]
    with_links = [r for r in rows if candidates[r["query"]]]
    officials = [(r, link) for r in rows for link in candidates[r["query"]] if link["kind"] == "official"]
    official_in_staan = sum(host(link["url"]) in {host(u) for u in r["lists"]["staan"]} for r, link in officials)
    short_with_links = [r for r in with_links if len(r["lists"]["staan"]) < NUM_RESULTS]
    print(f"\n# Wikidata coverage ({len(rows)} en-gb queries)\n")
    print(f"- Any wbsearchentities hit: {sum(bool(raw[r['query']]) for r in rows)}")
    print(f"- A hit whose label or alias is exactly the query: {len(matched)}")
    print(
        f"- ...with at least one usable link: {len(with_links)}, of which Staan returned fewer than ten for {len(short_with_links)}"
    )
    print(f"- ...with an official website: {len(officials)}, whose host Staan already returned for {official_in_staan}")
    kinds = Counter(link["kind"] for links in candidates.values() for link in links)
    print("- Links by kind: " + ", ".join(f"{kind} {n}" for kind, n in kinds.most_common()))


def report_anchors(uk_anchors: list, p3_anchors: list) -> None:
    print("\n# Anchors: the new run's grades for URLs already graded\n")
    print(
        "| Judge | Anchors | Exact | Within 1 | Mean, original | Mean, new run | Correlation |\n|---|---|---|---|---|---|---|"
    )
    for name, anchors, field in (
        ("UK relevance (0-3)", uk_anchors, "relevance"),
        ("Pass-3 overall (0-10)", p3_anchors, "overall"),
    ):
        a = np.array([(old[field], new[field]) for old, new in anchors])
        print(
            f"| {name} | {len(a)} | {np.mean(a[:, 0] == a[:, 1]):.0%} | {np.mean(abs(a[:, 0] - a[:, 1]) <= 1):.0%} | "
            f"{a[:, 0].mean():.2f} | {a[:, 1].mean():.2f} | {np.corrcoef(a.T)[0, 1]:.2f} |"
        )


def report_arms(rows: list[dict], candidates: dict[str, list[dict]], relevance: dict, pass3: dict) -> None:
    ltr_and_wiki = ltr_and_wikipedia_by_minilm()
    uk: dict[str, list[float]] = {}
    p3: dict[str, dict[str, list[float]]] = {}
    changed = []
    for row in rows:
        query = row["query"]
        arms = wikidata_arms(row, candidates[query], ltr_and_wiki[query])
        needed = {url for urls in arms.values() for url in urls}
        graded = relevance.get(query, {})
        if needed <= set(graded):
            all_gains = [2 ** g["relevance"] - 1 for g in graded.values()]
            for arm, urls in arms.items():
                uk.setdefault(arm, []).append(ndcg([2 ** graded[u]["relevance"] - 1 for u in urls], all_gains))
        judged = pass3.get(query, {})
        if needed <= set(judged):
            changed.append(arms[ALL_LINKS] != arms[BASELINE])
            for arm, urls in arms.items():
                metrics = p3.setdefault(arm, {"overall": [], "relevance": [], "ethos": []})
                metrics["overall"].append(
                    ndcg([judged[u]["overall"] for u in urls], [j["overall"] for j in judged.values()])
                )
                metrics["relevance"].append(
                    ndcg(
                        [2 ** judged[u]["relevance"] - 1 for u in urls],
                        [2 ** j["relevance"] - 1 for j in judged.values()],
                    )
                )
                ethos = np.array([judged[u]["ethos"] for u in urls])
                weights = DISCOUNTS[: len(ethos)]
                metrics["ethos"].append(float(np.sum(ethos * weights) / np.sum(weights)) if len(ethos) else 0.0)

    rng = np.random.default_rng(0)
    changed = np.array(changed)
    print(f"\n# UK relevance NDCG@10 ({len(uk[BASELINE])} queries)\n")
    print(f"| Arm | NDCG | vs {BASELINE} | vs {MINILM_FILL} |\n|---|---|---|---|")
    for arm, values in uk.items():
        v = np.array(values)
        print(
            f"| {arm} | {v.mean():.3f} | {bootstrap(v - np.array(uk[BASELINE]), rng)} | {bootstrap(v - np.array(uk[MINILM_FILL]), rng)} |"
        )

    print(
        f"\n# Pass-3 judge ({len(p3[BASELINE]['overall'])} queries; Wikidata changes the top ten of {changed.sum()})\n"
    )
    print(
        f"| Arm | Overall NDCG | Relevance NDCG | Ethos | Overall vs {BASELINE} | ...on the {changed.sum()} changed queries | "
        f"Overall vs {MINILM_FILL} |\n|---|---|---|---|---|---|---|"
    )
    base, minilm = np.array(p3[BASELINE]["overall"]), np.array(p3[MINILM_FILL]["overall"])
    for arm, metrics in p3.items():
        overall = np.array(metrics["overall"])
        print(
            f"| {arm} | {overall.mean():.3f} | {np.mean(metrics['relevance']):.3f} | {np.mean(metrics['ethos']):.2f} | "
            f"{bootstrap(overall - base, rng)} | {bootstrap((overall - base)[changed], rng)} | {bootstrap(overall - minilm, rng)} |"
        )


def report_by_kind(candidates: dict[str, list[dict]]) -> None:
    uk = {
        (r["query"], r["url"]): r
        for r in map(json.loads, (EVAL_DIR / "haiku/relevance_engb_wikidata.jsonl").read_text().splitlines())
    }
    p3 = {
        (r["query"], r["url"]): r
        for r in map(json.loads, (EVAL_DIR / "haiku/pass3_engb_wikidata.jsonl").read_text().splitlines())
    }
    by_kind: dict[str, list[tuple[int, int, int, int]]] = {}
    for query, links in candidates.items():
        for link in links:
            key = (query, link["url"])
            if key in p3 and not p3[key]["anchor"]:
                j = p3[key]
                by_kind.setdefault(link["kind"], []).append(
                    (j["relevance"], j["ethos"], j["overall"], uk[key]["relevance"])
                )
    print("\n# The newly graded Wikidata URLs, by kind\n")
    print("| Kind | URLs | Pass-3 relevance | Ethos | Overall | UK relevance |\n|---|---|---|---|---|---|")
    for kind, values in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        v = np.array(values, dtype=float)
        print(
            f"| {kind} | {len(v)} | {v[:, 0].mean():.2f} | {v[:, 1].mean():.2f} | {v[:, 2].mean():.1f} | {v[:, 3].mean():.2f} |"
        )


if __name__ == "__main__":
    report_short_staan()
    report_page2()
    engb_rows = load_json("engb/rows-0.05.json")
    wikidata = wikidata_candidates()
    report_coverage(engb_rows, wikidata)
    uk_judgments, uk_anchors = merged_judgments("haiku/relevance_engb.jsonl", "haiku/relevance_engb_wikidata.jsonl")
    p3_judgments, p3_anchors = merged_judgments("haiku/pass3_engb.jsonl", "haiku/pass3_engb_wikidata.jsonl")
    report_anchors(uk_anchors, p3_anchors)
    report_arms(engb_rows, wikidata, uk_judgments, p3_judgments)
    report_by_kind(wikidata)
