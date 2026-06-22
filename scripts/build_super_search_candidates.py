#!/usr/bin/env python3
"""Build super-search-candidates.json by enriching curated-domains.json.

Two-phase helper for the Super Search v2 candidate analysis:

  prepare  - read curated-domains.json, compute an HN-derived popularity prior for
             each domain, and split the domains into input chunks under
             candidate_chunks/input_NNN.json for the classifier subagents to consume.

  merge    - read the classified chunk_NNN.json files, validate them against the
             schema/taxonomy, merge in original order, and write
             super-search-candidates.json. Prints distribution stats.

  shortlist       - filter candidates to recommended + has_search + non-obscure.

  select_targets  - pick ~140 shortlist sites that still need a recipe (excluding
                    those already served by an adapter/recipe), maximising field
                    diversity, and split them into small batches under
                    recipe_chunks/input_NNN.json for the recipe sub-agent fan-out.

Run from the repo root (the directory containing curated-domains.json).
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
CURATED = REPO_ROOT / "curated-domains.json"
CHUNK_DIR = REPO_ROOT / "candidate_chunks"
OUTPUT = REPO_ROOT / "super-search-candidates.json"
SHORTLIST = REPO_ROOT / "super-search-shortlist.json"
DEVDATA_SHORTLIST = REPO_ROOT / "devdata" / "super-search-shortlist.json"
RECIPES_DIR = REPO_ROOT / "mwmbl" / "tinysearchengine" / "super_search_sources" / "recipes"
TARGETS = REPO_ROOT / "super-search-targets.json"
RECIPE_CHUNK_DIR = REPO_ROOT / "recipe_chunks"

CHUNK_SIZE = 150
# Over-select so ~20-30% dropped (API key / no clean endpoint / un-passable smoke)
# still leaves ~100 landed recipes.
TARGET_COUNT = 140
RECIPE_BATCH_SIZE = 5

# Domains already served by a hand-written adapter (recipe domains are detected
# from the YAML files). The Stack Exchange adapter covers the whole SE network.
ADAPTER_DOMAINS = {
    "mwmbl.org", "news.ycombinator.com", "github.com", "pypi.org",
    "arxiv.org", "info.arxiv.org",
    "stackoverflow.com", "stackexchange.com", "superuser.com",
    "mathoverflow.net", "serverfault.com", "askubuntu.com",
}

# Fixed taxonomy / enums (keep in sync with the subagent prompt).
FIELDS = {
    "programming", "tech", "science", "academia", "books-literature",
    "recipes-food", "history", "art-design", "gaming", "music", "film-tv",
    "business-finance", "health-medicine", "news-politics", "law-government",
    "education", "philosophy", "nature-environment", "sports", "lifestyle",
    "other",
}
LEVELS = {"low", "medium", "high"}
SITE_TYPES = {
    "blog", "docs", "wiki", "forum", "journal", "news", "store",
    "personal", "reference", "tool", "org",
}
REQUIRED_KEYS = {
    "name", "field", "popularity", "estimated_pages", "site_type",
    "language", "has_search", "recommended", "reason",
}

# Map classifier synonyms onto the fixed taxonomy (keeps the output reproducible
# without re-running the subagents).
NORMALIZE_FIELD = {
    "reference": "other",
    "personal": "other",
    "organization": "other",
    "forum": "tech",
    "productivity": "tech",
    "math": "science",
    "mathematics": "science",
    "politics": "news-politics",
    "science-fiction": "books-literature",
}
NORMALIZE_SITE_TYPE = {
    "archive": "reference",
    "database": "reference",
    "library": "reference",
    "educational": "docs",
    "conference": "org",
    "other": "reference",
}


def load_curated():
    data = json.loads(CURATED.read_text())
    return [e["name"] for e in data["domains"]]


def root_domain(d):
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


def hn_prior_map(names):
    """Map domain -> 'high' | 'medium' hint from HN top-domain scores, else absent."""
    from mwmbl.hn_top_domains_filtered import DOMAINS
    hn = {k.lower(): v for k, v in DOMAINS.items()}
    prior = {}
    for name in names:
        lname = name.lower()
        score = hn.get(lname)
        if score is None:
            score = hn.get(root_domain(lname))
        if score is None:
            continue
        prior[name] = "high" if score >= 0.95 else "medium"
    return prior


def cmd_prepare():
    names = load_curated()
    prior = hn_prior_map(names)
    CHUNK_DIR.mkdir(exist_ok=True)
    (CHUNK_DIR / "_hn_prior.json").write_text(json.dumps(prior, indent=2))

    n_chunks = 0
    for i in range(0, len(names), CHUNK_SIZE):
        chunk = names[i:i + CHUNK_SIZE]
        idx = i // CHUNK_SIZE
        payload = [
            {"name": name, "hn_popularity_hint": prior.get(name)}
            for name in chunk
        ]
        (CHUNK_DIR / f"input_{idx:03d}.json").write_text(json.dumps(payload, indent=2))
        n_chunks += 1
    print(f"Prepared {len(names)} domains into {n_chunks} input chunks "
          f"(size {CHUNK_SIZE}) in {CHUNK_DIR}")
    print(f"HN popularity prior available for {len(prior)} domains.")


def cmd_merge():
    names = load_curated()
    by_name = {}
    errors = []

    for path in sorted(CHUNK_DIR.glob("chunk_*.json")):
        try:
            entries = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"{path.name}: invalid JSON ({e})")
            continue
        for entry in entries:
            if entry.get("field") in NORMALIZE_FIELD:
                entry["field"] = NORMALIZE_FIELD[entry["field"]]
            if entry.get("site_type") in NORMALIZE_SITE_TYPE:
                entry["site_type"] = NORMALIZE_SITE_TYPE[entry["site_type"]]
            missing = REQUIRED_KEYS - entry.keys()
            extra = entry.keys() - REQUIRED_KEYS
            if missing:
                errors.append(f"{entry.get('name', '?')}: missing keys {sorted(missing)}")
            if extra:
                errors.append(f"{entry.get('name', '?')}: unexpected keys {sorted(extra)}")
            if entry.get("field") not in FIELDS:
                errors.append(f"{entry.get('name')}: bad field {entry.get('field')!r}")
            if entry.get("popularity") not in LEVELS:
                errors.append(f"{entry.get('name')}: bad popularity {entry.get('popularity')!r}")
            if entry.get("estimated_pages") not in LEVELS:
                errors.append(f"{entry.get('name')}: bad estimated_pages {entry.get('estimated_pages')!r}")
            if entry.get("site_type") not in SITE_TYPES:
                errors.append(f"{entry.get('name')}: bad site_type {entry.get('site_type')!r}")
            if not isinstance(entry.get("has_search"), bool):
                errors.append(f"{entry.get('name')}: has_search not bool")
            if not isinstance(entry.get("recommended"), bool):
                errors.append(f"{entry.get('name')}: recommended not bool")
            name = entry.get("name")
            if name in by_name:
                errors.append(f"{name}: duplicate entry")
            by_name[name] = entry

    curated_set = set(names)
    missing_names = [n for n in names if n not in by_name]
    extra_names = [n for n in by_name if n not in curated_set]

    print(f"Classified {len(by_name)} / {len(names)} domains, "
          f"{len(missing_names)} missing, {len(errors)} invalid, "
          f"{len(extra_names)} unexpected.")

    if missing_names:
        print("  Missing (first 20):", missing_names[:20])
    if extra_names:
        print("  Unexpected (first 20):", extra_names[:20])
    if errors:
        print("  Errors (first 30):")
        for e in errors[:30]:
            print("   ", e)

    if missing_names or extra_names or errors:
        print("\nNot writing output until issues are resolved.", file=sys.stderr)
        return 1

    merged = [by_name[n] for n in names]
    OUTPUT.write_text(json.dumps({"domains": merged}, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {OUTPUT} with {len(merged)} domains.")

    print("\n-- field --")
    for k, v in Counter(e["field"] for e in merged).most_common():
        print(f"  {k:20} {v}")
    print("-- popularity --")
    for k, v in Counter(e["popularity"] for e in merged).most_common():
        print(f"  {k:20} {v}")
    print("-- estimated_pages --")
    for k, v in Counter(e["estimated_pages"] for e in merged).most_common():
        print(f"  {k:20} {v}")
    print("-- site_type --")
    for k, v in Counter(e["site_type"] for e in merged).most_common():
        print(f"  {k:20} {v}")
    rec = sum(1 for e in merged if e["recommended"])
    has = sum(1 for e in merged if e["has_search"])
    print(f"\nrecommended: {rec}/{len(merged)}   has_search: {has}/{len(merged)}")
    return 0


def cmd_shortlist():
    """Select v2 source candidates: recommended + has on-site search + non-obscure."""
    if not OUTPUT.exists():
        print("Run `merge` first to build super-search-candidates.json", file=sys.stderr)
        return 1
    domains = json.loads(OUTPUT.read_text())["domains"]
    pop_rank = {"high": 0, "medium": 1, "low": 2}
    shortlist = [
        e for e in domains
        if e["recommended"] and e["has_search"]
        and e["popularity"] in ("medium", "high")
    ]
    shortlist.sort(key=lambda e: (pop_rank[e["popularity"]], e["field"], e["name"]))
    SHORTLIST.write_text(
        json.dumps({"domains": shortlist}, indent=2, ensure_ascii=False) + "\n"
    )
    print(f"Wrote {SHORTLIST} with {len(shortlist)} / {len(domains)} domains.")
    print("Filter: recommended AND has_search AND popularity in {medium, high}")
    print("\n-- field --")
    for k, v in Counter(e["field"] for e in shortlist).most_common():
        print(f"  {k:20} {v}")
    print("-- popularity --")
    for k, v in Counter(e["popularity"] for e in shortlist).most_common():
        print(f"  {k:20} {v}")
    print("-- site_type --")
    for k, v in Counter(e["site_type"] for e in shortlist).most_common():
        print(f"  {k:20} {v}")
    return 0


def _covered_domains():
    """Domains already served: hand-written adapters + existing recipe YAML files."""
    import yaml
    covered = set(ADAPTER_DOMAINS)
    for path in RECIPES_DIR.glob("*.yaml"):
        try:
            covered.add(yaml.safe_load(path.read_text())["domain"])
        except (KeyError, yaml.YAMLError, OSError):
            continue
    return covered


def _select_diverse(entries, count):
    """Round-robin across fields (popularity-first within a field) to maximise diversity."""
    pop_rank = {"high": 0, "medium": 1, "low": 2}
    by_field = {}
    for e in entries:
        by_field.setdefault(e["field"], []).append(e)
    for field_entries in by_field.values():
        field_entries.sort(key=lambda e: (pop_rank[e["popularity"]], e["name"]))
    selected, fields = [], sorted(by_field)
    while len(selected) < count and any(by_field.values()):
        for field in fields:
            if by_field[field]:
                selected.append(by_field[field].pop(0))
                if len(selected) >= count:
                    break
    return selected


def cmd_select_targets():
    """Pick ~TARGET_COUNT shortlist sites needing a recipe, batched for sub-agents."""
    path = DEVDATA_SHORTLIST if DEVDATA_SHORTLIST.exists() else SHORTLIST
    if not path.exists():
        print("No shortlist found; run `shortlist` first.", file=sys.stderr)
        return 1
    entries = json.loads(path.read_text())["domains"]
    covered = _covered_domains()
    candidates = [e for e in entries if e["name"] not in covered]
    targets = _select_diverse(candidates, TARGET_COUNT)

    TARGETS.write_text(json.dumps({"domains": targets}, indent=2, ensure_ascii=False) + "\n")

    RECIPE_CHUNK_DIR.mkdir(exist_ok=True)
    for old in RECIPE_CHUNK_DIR.glob("input_*.json"):
        old.unlink()
    n_batches = 0
    for i in range(0, len(targets), RECIPE_BATCH_SIZE):
        batch = targets[i:i + RECIPE_BATCH_SIZE]
        (RECIPE_CHUNK_DIR / f"input_{i // RECIPE_BATCH_SIZE:03d}.json").write_text(
            json.dumps(batch, indent=2, ensure_ascii=False)
        )
        n_batches += 1

    print(f"Selected {len(targets)} targets from {len(candidates)} uncovered "
          f"shortlist sites ({len(covered)} already covered).")
    print(f"Wrote {TARGETS} and {n_batches} batches (size {RECIPE_BATCH_SIZE}) "
          f"in {RECIPE_CHUNK_DIR} for the recipe sub-agent fan-out.")
    print("\n-- field --")
    for k, v in Counter(e["field"] for e in targets).most_common():
        print(f"  {k:20} {v}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["prepare", "merge", "shortlist", "select_targets"]
    )
    args = parser.parse_args()
    if args.command == "prepare":
        cmd_prepare()
        return 0
    if args.command == "shortlist":
        return cmd_shortlist()
    if args.command == "select_targets":
        return cmd_select_targets()
    return cmd_merge()


if __name__ == "__main__":
    sys.exit(main())
