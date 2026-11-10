"""Index the Jev-approved Commons images into a copy of a search index, and see what standard search then shows.

An image is indexed as a page of its own: its Commons file page is the URL, its file name the title and its
description, optionally followed by its categories, the extract. It is indexed under its own terms the way a
crawled page is, never against the query that found it. Only the images of the 849 train queries are indexed,
and the 295 en-gb queries are searched with standard search's ranker (LTR + MMR) on the original index and on
each variant. Wikipedia's results, which standard search adds, are fetched once per query and shared by every arm.

Run with `.env` sourced, DJANGO_SETTINGS_MODULE=mwmbl.settings_dev and EXTERNAL_CACHE_ENABLED=false, so the
Wikipedia fetches never write the dev cache, and PYTHONHASHSEED=0: with a random seed, tied results swap places.

    build NAME INDEX    copy INDEX to indexing/NAME/original.tinysearch and once per variant, and index the
                        images into each variant's copy only, measuring the existing documents they evict
    search NAME         each arm's en-gb lists -> indexing/NAME/lists_<arm>.json
    relevance NAME      Jev image relevance of each (en-gb query, image) pair in a top ten, cached
    report NAME         how the top tens change -> indexing/NAME/report.json
    batches NAME ARM    holistic-judge batches for ARM against the original -> indexing/NAME/<ARM>_vs_original
"""

import json
import os
import shutil
from argparse import ArgumentParser
from collections import Counter
from pathlib import Path

import django
from django.conf import settings

# judge reaches Django models through jev_rank, so the registry must be up first.
django.setup()

from mwmbl.imagesearch.judge import relevance_request, scores  # noqa: E402
from mwmbl.imagesearch.pipeline import file_id  # noqa: E402
from mwmbl.imagesearch.types import SourceImage  # noqa: E402
from mwmbl.indexer.index_batches import index_documents, index_pages, preprocess_documents  # noqa: E402
from mwmbl.rankeval.evaluation import holistic_judge  # noqa: E402
from mwmbl.rankeval.evaluation.jev_order import cached_response  # noqa: E402
from mwmbl.rankeval.seed_images import SEED_IMAGES_DIR  # noqa: E402
from mwmbl.rankeval.seed_images.collect import load_candidates  # noqa: E402
from mwmbl.rankeval.seed_images.gate_analysis import RECOMMENDED_PATH  # noqa: E402
from mwmbl.rankeval.seed_images.gate_data import RELEVANCE_PATH, jev_safe_candidates  # noqa: E402
from mwmbl.rankeval.seed_images.queries import NUM_RESULTS, load_queries, read_jsonl  # noqa: E402
from mwmbl.rankeval.seed_images.run_jev import CACHE_DIR, NEED_PATH  # noqa: E402
from mwmbl.tinysearchengine.completer import Completer  # noqa: E402
from mwmbl.tinysearchengine.indexer import (  # noqa: E402
    Document,
    DocumentSource,
    DocumentState,
    TinyIndex,
    cleaned_document,
)
from mwmbl.tinysearchengine.ltr import RustXGBPipeline  # noqa: E402
from mwmbl.tinysearchengine.ltr_rank import LTRRanker  # noqa: E402
from mwmbl.tinysearchengine.mmr_rank import MMRRanker  # noqa: E402
from mwmbl.tinysearchengine.rank import get_wiki_results  # noqa: E402

INDEXING_DIR = SEED_IMAGES_DIR / "indexing"
WIKI_PATH = INDEXING_DIR / "wiki.json"
RELEVANCE_FILE = "image_relevance.json"
ORIGINAL = "original"
# Variant name -> (whether the extract carries the categories after the description, whether an image is written
# only to the pages of terms the index already holds). The second leaves out the images' other terms, whose pages
# a 65,536-page test index shares with the queried terms far more often than production's 102M-page index does.
VARIANTS = {
    "description": (False, False),
    "description_categories": (True, False),
    "description_same_term": (False, True),
    "description_categories_same_term": (True, True),
}
COMMONS_FILE_PREFIX = "https://commons.wikimedia.org/wiki/File:"
# The crawler's limits (mwmbl.crawler.retrieve), which every page in the index is cut to.
NUM_TITLE_CHARS = 65
NUM_EXTRACT_CHARS = 155
CATEGORY_SEPARATOR = " · "


def truncated(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def image_document(image: SourceImage, with_categories: bool) -> Document:
    name = image.title.rsplit(".", 1)[0].replace("_", " ")
    parts = [image.description, *image.categories] if with_categories else [image.description]
    extract = CATEGORY_SEPARATOR.join(part.strip() for part in parts if part.strip())
    return Document(truncated(name, NUM_TITLE_CHARS), image.page_url, truncated(extract, NUM_EXTRACT_CHARS))


def is_image(url: str) -> bool:
    return url.startswith(COMMONS_FILE_PREFIX)


def approved_train_images() -> list[SourceImage]:
    """The Jev-safe candidates of the train queries, each once, in query order."""
    queries = load_queries()
    safe = jev_safe_candidates()
    by_url = {}
    for query, entry in queries.items():
        if entry["set"] == "train":
            for image in safe.get(query, []):
                by_url.setdefault(image.page_url, image)
    return list(by_url.values())


def held_pairs(index_path: Path, pages: list[int]) -> set[tuple[int, str, str]]:
    with TinyIndex(Document, index_path) as index:
        return {(page, document.term, document.url) for page in pages for document in index.get_page(page)}


def held_terms(page_documents: dict[int, list[Document]], held: set[tuple[int, str, str]]) -> dict:
    """The page documents whose term the page already holds, leaving out the pages with none."""
    terms = {(page, term) for page, term, _ in held}
    kept = {page: [d for d in documents if (page, d.term) in terms] for page, documents in page_documents.items()}
    return {page: documents for page, documents in kept.items() if documents}


def build(name: str, index_path: Path) -> None:
    work = INDEXING_DIR / name
    work.mkdir(parents=True, exist_ok=True)
    original_path = work / f"{ORIGINAL}.tinysearch"
    shutil.copyfile(index_path, original_path)
    images = approved_train_images()
    stats = {"images": len(images)}
    for variant, (with_categories, same_term_only) in VARIANTS.items():
        variant_path = work / f"{variant}.tinysearch"
        shutil.copyfile(original_path, variant_path)
        documents = [cleaned_document(image_document(image, with_categories)) for image in images]
        page_documents = preprocess_documents(documents, variant_path)
        pages = sorted(page_documents)
        before = held_pairs(variant_path, pages)
        if same_term_only:
            page_documents = held_terms(page_documents, before)
            index_pages(str(variant_path), page_documents)
        else:
            index_documents(documents, variant_path)
        after = held_pairs(variant_path, pages)
        stored_images = {pair for pair in after if is_image(pair[2])}
        image_terms = {(page, term) for page, term, _ in stored_images}
        evicted = before - after
        stats[variant] = {
            "image_term_documents_submitted": sum(len(docs) for docs in page_documents.values()),
            "image_term_documents_stored": len(stored_images),
            "image_terms": len({term for _, term, _ in stored_images}),
            "existing_terms_given_images": len({term for page, term, _ in before if (page, term) in image_terms}),
            "existing_documents_on_written_pages": len(before),
            "existing_documents_evicted": len(evicted),
            "evicted_by_own_term_images": sum((page, term) in image_terms for page, term, _ in evicted),
            "evicted_terms": len({term for _, term, _ in evicted}),
            "images_per_term_top": Counter(term for _, term, _ in stored_images).most_common(25),
        }
        print(variant, {key: value for key, value in stats[variant].items() if key != "images_per_term_top"})
    (work / "build.json").write_text(json.dumps(stats, indent=1))


def engb_queries() -> list[str]:
    return [query for query, entry in load_queries().items() if entry["set"] == "engb"]


def wiki_documents(entries: list[list]) -> list[Document]:
    return [
        Document(title, url, extract, score, term, state=DocumentState.FROM_WIKI, source=DocumentSource.WIKIPEDIA)
        for title, url, extract, score, term in entries
    ]


def fetch_wiki(queries: list[str]) -> dict[str, list[list]]:
    assert not settings.EXTERNAL_CACHE_ENABLED, "set EXTERNAL_CACHE_ENABLED=false: don't write the dev cache"
    wiki = json.loads(WIKI_PATH.read_text()) if WIKI_PATH.exists() else {}
    for query in queries:
        if query not in wiki:
            results = get_wiki_results(query, wait_out_rate_limits=True)
            wiki[query] = [[d.title, d.url, d.extract, d.score, d.term] for d in results]
            WIKI_PATH.write_text(json.dumps(wiki))
    return wiki


def search(name: str) -> None:
    assert os.environ.get("PYTHONHASHSEED") == "0", "set PYTHONHASHSEED=0: with a random seed, tied results swap places"
    queries = engb_queries()
    wiki = fetch_wiki(queries)
    model = RustXGBPipeline.from_model_path(str(settings.RUST_MODEL_PATH))
    completer = Completer()
    for arm in [ORIGINAL, *VARIANTS]:
        with TinyIndex(Document, INDEXING_DIR / name / f"{arm}.tinysearch") as index:
            ranker = MMRRanker(LTRRanker(index, completer, model, include_wiki=False))
            # Passing Wikipedia's results in is what include_wiki would do, from the one fetch every arm shares.
            lists = {
                query: [
                    {"url": d.url, "title": d.title, "extract": d.extract}
                    for d in ranker.search(query, wiki_documents(wiki[query]))[:NUM_RESULTS]
                ]
                for query in queries
            }
        (INDEXING_DIR / name / f"lists_{arm}.json").write_text(json.dumps(lists))
        print(f"{arm}: {sum(any(is_image(r['url']) for r in rs) for rs in lists.values())} queries show an image")


def candidates_by_url() -> dict[str, SourceImage]:
    return {image.page_url: image for query in load_queries() for image in load_candidates(query)}


def relevance(name: str) -> None:
    """Jev image relevance for each image in an en-gb top ten: the gate's score if it has one, else asked now."""
    known = {
        (row["query"], fid): s for row in read_jsonl(RELEVANCE_PATH) for fid, s in zip(row["file_ids"], row["scores"])
    }
    images = candidates_by_url()
    shown: dict[str, set[str]] = {}
    for arm in VARIANTS:
        for query, results in json.loads((INDEXING_DIR / name / f"lists_{arm}.json").read_text()).items():
            shown.setdefault(query, set()).update(r["url"] for r in results if is_image(r["url"]))
    scored = {}
    for query, urls in shown.items():
        unscored = sorted(url for url in urls if (query, file_id(images[url])) not in known)
        if unscored:
            unscored_images = [images[url] for url in unscored]
            asked = scores(cached_response(relevance_request(query, unscored_images), CACHE_DIR), "i", len(unscored))
            known |= {(query, file_id(image)): s for image, s in zip(unscored_images, asked)}
        scored[query] = {url: known[(query, file_id(images[url]))] for url in urls}
    (INDEXING_DIR / name / RELEVANCE_FILE).write_text(json.dumps(scored))


def top_ten_change(original: list[dict], augmented: list[dict]) -> dict:
    """The images in the augmented top ten, at their positions, and the web results they pushed out."""
    augmented_urls = {result["url"] for result in augmented}
    return {
        "images": [(position, r["url"]) for position, r in enumerate(augmented, 1) if is_image(r["url"])],
        "displaced": [r["url"] for r in original if r["url"] not in augmented_urls],
        "changed": [r["url"] for r in original] != [r["url"] for r in augmented],
    }


def report(name: str) -> None:
    work = INDEXING_DIR / name
    original = json.loads((work / f"lists_{ORIGINAL}.json").read_text())
    image_relevance = json.loads((work / RELEVANCE_FILE).read_text())
    need = {row["query"]: row["score"] for row in read_jsonl(NEED_PATH)}
    gate = json.loads(RECOMMENDED_PATH.read_text())
    summary = {}
    for arm in VARIANTS:
        augmented = json.loads((work / f"lists_{arm}.json").read_text())
        changes = {query: top_ten_change(original[query], augmented[query]) for query in original}
        shown = [(query, position, url) for query, c in changes.items() for position, url in c["images"]]
        shown_relevance = [image_relevance[query][url] for query, _, url in shown]
        summary[arm] = {
            "queries_changed": sum(c["changed"] for c in changes.values()),
            "queries_with_images": sum(bool(c["images"]) for c in changes.values()),
            "images_shown": len(shown),
            "positions": Counter(position for _, position, _ in shown),
            "relevance_ge_1_5": sum(s >= gate["min_jev_relevance"] for s in shown_relevance),
            "relevance_lt_1": sum(s < 1 for s in shown_relevance),
            "images_on_gated_out_queries": sum(need[query] < gate["jev_need_threshold"] for query, _, _ in shown),
            "queries_changed_without_images": sum(c["changed"] and not c["images"] for c in changes.values()),
            "web_results_displaced": sum(len(c["displaced"]) for c in changes.values()),
            "web_results_displaced_by_images": sum(len(c["displaced"]) for c in changes.values() if c["images"]),
            "shown": [
                {"query": q, "position": p, "url": u, "jev_relevance": image_relevance[q][u], "jev_need": need[q]}
                for q, p, u in shown
            ],
            "displaced": {query: c["displaced"] for query, c in changes.items() if c["displaced"]},
        }
        print(arm, {key: value for key, value in summary[arm].items() if key not in ("shown", "displaced")})
    (work / "report.json").write_text(json.dumps(summary, indent=1))


def judge_batches(name: str, arm: str) -> None:
    work = INDEXING_DIR / name
    augmented = json.loads((work / f"lists_{arm}.json").read_text())
    images = candidates_by_url()
    # The holistic judge marks a single picture by "image" and reads its description as the extract.
    marked = {
        query: [r | {"extract": images[r["url"]].description, "image": True} if is_image(r["url"]) else r for r in rs]
        for query, rs in augmented.items()
    }
    marked_path = work / f"lists_{arm}_judged.json"
    marked_path.write_text(json.dumps(marked))
    holistic_judge.batches(marked_path, work / f"lists_{ORIGINAL}.json", work / f"{arm}_vs_{ORIGINAL}")


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["build", "search", "relevance", "report", "batches"])
    parser.add_argument("name")
    parser.add_argument("extra", nargs="?", help="build: the index to copy; batches: the arm to judge")
    args = parser.parse_args()

    if args.command == "build":
        build(args.name, Path(args.extra))
    elif args.command == "batches":
        judge_batches(args.name, args.extra)
    else:
        commands = {"search": search, "relevance": relevance, "report": report}
        commands[args.command](args.name)


if __name__ == "__main__":
    main()
