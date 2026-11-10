"""LTR training rows for Commons image pages: the Jev-safe images in the LLM-labelled queries' pools (#610).

Standard search's LTR has never seen an image page. This puts the Jev-safe images (NSFW below 1.0) of the 849
train queries, the set indexing.py indexes, where indexing them would put them, grades the ones that reach an
LLM-labelled query's candidates with the pass-3 prompt, and writes the LLM dataset with those rows added, tagged
with their own pool so a model can be trained with and without them (`llm_experiment --exclude-pool commons_image`).
The en-gb queries' images stay out, so an en-gb evaluation never ranks images its own queries found.

A query's images are not cut to the top 30 by stored score as its pass-2 standard rows were: an image's stored
score is None, so its place among the other None-scored pages would be arbitrary, and the LTR scores every
retrieved candidate at serving time anyway.

Each image is the document indexing.py made of it (file page URL, file name as title, description as extract, no
categories) under each of its own terms, never the query that found it. Its documents are merged into production's
pages, read whole from api.mwmbl.org, with the indexer's own write order and page size, so an image is kept only
where it would really fit, and it competes with the other terms sharing its page as well as its own. A query's
images are those its lookup terms (terms and bigrams, as the pass-2 standard pool looked them up) then retrieve,
less any the majority-terms rule would drop at serving time, which no model could rank.

Run from the repo root with DJANGO_SETTINGS_MODULE=mwmbl.settings_dev:

    overlay                 -> ltr/pools.jsonl, every LLM query with the images it retrieves
    batches TAG N           pass-3 batches of the ungraded images, with graded anchors, -> ltr/work/TAG/
    merge TAG FILE...       the judges' output -> ltr/pass3_judgments.jsonl and ltr/pass3_anchors.jsonl
    build                   -> ltr/learning-to-rank-llm-images.csv.gz, the LLM dataset plus the graded image rows
"""

import json
import random
from argparse import ArgumentParser
from collections import Counter, defaultdict
from pathlib import Path

import django
import mmh3
import pandas as pd

# jev_images reaches Django models through jev_rank, so the registry must be up first.
django.setup()

from mwmbl.crawler.env_vars import MWMBL_REMOTE_SERVER  # noqa: E402
from mwmbl.imagesearch.types import SourceImage  # noqa: E402
from mwmbl.indexer.index import tokenize_document  # noqa: E402
from mwmbl.indexer.index_batches import combine_documents  # noqa: E402
from mwmbl.rankeval.evaluation.remote_index import USER_AGENT  # noqa: E402
from mwmbl.rankeval.paths import LEARNING_TO_RANK_LLM_DATASET_PATH  # noqa: E402
from mwmbl.rankeval.seed_images import SEED_IMAGES_DIR  # noqa: E402
from mwmbl.rankeval.seed_images.indexing import approved_train_images, image_document  # noqa: E402
from mwmbl.rankeval.seed_images.queries import LLM_RELABEL_DIR, read_jsonl  # noqa: E402
from mwmbl.tinysearchengine.indexer import PAGE_SIZE, Document, cleaned_document  # noqa: E402
from mwmbl.tinysearchengine.rank import HeuristicRanker, get_features  # noqa: E402
from mwmbl.tokenizer import get_bigrams, tokenize  # noqa: E402
from mwmbl.utils import request_cache  # noqa: E402
from mwmbl_rank import pack_index_page  # noqa: E402
from scripts.llm_relabel_pass3_judge import EXTRACT_CHARS, JUDGE_PROMPT  # noqa: E402

LTR_DIR = SEED_IMAGES_DIR / "ltr"
POOLS_PATH = LTR_DIR / "pools.jsonl"
WORK_DIR = LTR_DIR / "work"
JUDGMENTS_PATH = LTR_DIR / "pass3_judgments.jsonl"
ANCHORS_PATH = LTR_DIR / "pass3_anchors.jsonl"
DATASET_PATH = LTR_DIR / "learning-to-rank-llm-images.csv.gz"
PASS2_POOL_PATH = LLM_RELABEL_DIR / "pass2_pool.jsonl"
PASS3_PATH = LLM_RELABEL_DIR / "pass3_judgments.jsonl"
IMAGE_POOL = "commons_image"
AXES = ("relevance", "ethos", "overall")
# settings_prod.NUM_PAGES. The page a term is stored on is its hash modulo this, so it must be production's.
PRODUCTION_NUM_PAGES = 102_400_000
# Web pages from the query's graded pool, judged again alongside its images: a check that grades given beside
# pictures are on the same scale as the pass-3 grades they are trained with.
NUM_ANCHORS = 2


def page_index(term: str) -> int:
    return mmh3.hash(term, signed=False) % PRODUCTION_NUM_PAGES


def image_term_documents(images: list[SourceImage]) -> dict[int, list[Document]]:
    """Production page -> the image documents indexing would send it, one per (image, term)."""
    pages = defaultdict(list)
    for image in images:
        document = cleaned_document(image_document(image, with_categories=False))
        tokenized = tokenize_document(document.url, document.title, document.extract, document.score)
        for term in tokenized.tokens:
            pages[page_index(term)].append(Document(document.title, document.url, document.extract, term=term))
    return pages


def remote_page(number: int) -> list[Document]:
    with request_cache() as session:
        session.headers["User-Agent"] = USER_AGENT
        response = session.get(f"{MWMBL_REMOTE_SERVER}/api/v1/search/page/{number}", timeout=30)
    response.raise_for_status()
    return [Document(*item) for item in response.json()]


def stored_page(existing: list[Document], incoming: list[Document]) -> list[Document]:
    """What a page holding `existing` stores once `incoming` is written to it, as _store_pages writes it."""
    ranker = HeuristicRanker(None, None, score_threshold=float("-inf"))
    combined = combine_documents(existing, incoming, False, ranker)
    _, num_stored = pack_index_page(PAGE_SIZE, [document.as_tuple() for document in combined])
    return combined[:num_stored]


def lookup_terms(terms: list[str]) -> set[str]:
    """The keys a complete query reads, as Ranker.retrieve reads them."""
    return set(terms) | set(get_bigrams(len(terms), terms))


def passes_majority_terms(terms: list[str], document: Document) -> bool:
    """The rule mwmbl_rank applies after scoring: a page matching at most half the terms always scores 0."""
    features = get_features(terms, document.title, document.url, document.extract, document.score, True)
    return features["match_terms"] > len(terms) / 2


def query_images(
    query: str, stored_pages: dict[int, list[Document]], image_urls: set[str], pool_urls: set[str]
) -> dict:
    """The overlaid images the query retrieves. Commons file pages production already holds are web rows: they
    went through pass 2 like any other page."""
    terms = tokenize(query)
    retrieved = {}
    for term in lookup_terms(terms):
        for document in stored_pages.get(page_index(term), []):
            if document.term == term and document.url in image_urls:
                retrieved.setdefault(document.url, document)
    kept = [d for d in retrieved.values() if passes_majority_terms(terms, d) and d.url not in pool_urls]
    return {
        "query": query,
        "retrieved": len(retrieved),
        "already_pooled": sum(url in pool_urls for url in retrieved),
        "images": [{"url": d.url, "title": d.title, "extract": d.extract} for d in kept],
    }


def overlay() -> None:
    pools = read_jsonl(PASS2_POOL_PATH)
    images = approved_train_images()
    image_urls = {image.page_url for image in images}
    image_pages = image_term_documents(images)
    wanted = sorted({page_index(term) for pool in pools for term in lookup_terms(tokenize(pool["query"]))})
    written = [number for number in wanted if number in image_pages]
    print(f"{len(written)} of the {len(wanted)} lookup pages receive images")
    stored_pages = {}
    evicted = 0
    for count, number in enumerate(written, 1):
        existing = remote_page(number)
        stored_pages[number] = stored_page(existing, image_pages[number])
        stored_urls = {(d.term, d.url) for d in stored_pages[number]}
        evicted += sum((d.term, d.url) not in stored_urls for d in existing)
        if count % 200 == 0:
            print(f"{count} pages read")
    LTR_DIR.mkdir(parents=True, exist_ok=True)
    with POOLS_PATH.open("w") as out:
        for pool in pools:
            pool_urls = {candidate["url"] for candidate in pool["candidates"]}
            out.write(json.dumps(query_images(pool["query"], stored_pages, image_urls, pool_urls)) + "\n")
    print(f"{evicted} existing documents evicted from those pages")


def graded_pairs(path: Path) -> set[tuple[str, str]]:
    return {(row["query"], row["url"]) for row in read_jsonl(path)} if path.exists() else set()


def anchors(pool: dict, grades: dict[tuple[str, str], dict]) -> list[dict]:
    graded = [c for c in pool["candidates"] if (pool["query"], c["url"]) in grades]
    return random.Random(pool["query"]).sample(graded, min(NUM_ANCHORS, len(graded)))


def candidate_line(number: int, candidate: dict) -> str:
    title = candidate["title"] or "(no title)"
    extract = (candidate["extract"] or "").replace("\n", " ")[:EXTRACT_CHARS]
    return f"{number}. {title} — {candidate['url']}\n   {extract}"


def batches(tag: str, num_batches: int) -> None:
    """Batches in the pass-3 judge's format, a query's images shuffled in with its anchors, blind as before."""
    intents = {row["query"]: row["intent"] for row in read_jsonl(LLM_RELABEL_DIR / "pass1_intents.jsonl")}
    pools = {pool["query"]: pool for pool in read_jsonl(PASS2_POOL_PATH)}
    grades = {(row["query"], row["url"]): row for row in read_jsonl(PASS3_PATH)}
    done = graded_pairs(JUDGMENTS_PATH)
    manifest = {}
    blocks = [[] for _ in range(num_batches)]
    todo = [row for row in read_jsonl(POOLS_PATH) if any((row["query"], i["url"]) not in done for i in row["images"])]
    for position, row in enumerate(todo):
        query = row["query"]
        images = [image | {"anchor": False} for image in row["images"] if (query, image["url"]) not in done]
        candidates = images + [anchor | {"anchor": True} for anchor in anchors(pools[query], grades)]
        random.Random(query).shuffle(candidates)
        lines = [f"\n=== QUERY: {query}\n=== INTENT: {intents[query]}"]
        for candidate in candidates:
            number = len(manifest) + 1
            manifest[number] = {"query": query, "url": candidate["url"], "anchor": candidate["anchor"]}
            lines.append(candidate_line(number, candidate))
        blocks[position % num_batches] += lines
    work = WORK_DIR / tag
    work.mkdir(parents=True, exist_ok=True)
    (work / "manifest.json").write_text(json.dumps(manifest))
    for number, lines in enumerate(blocks):
        (work / f"batch_{number:02d}.txt").write_text(JUDGE_PROMPT + "\n".join(lines) + "\n")
    print(f"{len(todo)} queries, {len(manifest)} candidates in {num_batches} batches -> {work}")


def parse_line(line: str) -> tuple[int, int, int, int] | None:
    """One `id || relevance || ethos || overall` line, or None for anything else, as the pass-3 merge reads it."""
    parts = [part.strip() for part in line.split("||")]
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return None
    number, relevance, ethos, overall = (int(part) for part in parts)
    if not (relevance <= 3 and ethos <= 3 and overall <= 10):
        return None
    return number, relevance, ethos, overall


def judged_lines(paths: list[Path], manifest: dict) -> dict[int, tuple[int, int, int]]:
    """Each manifest id's grades. Two different grades for one id fail: a candidate's text addressing the judge
    could print a line for another id, and keeping either line would hide that."""
    grades = {}
    for path in paths:
        for line in path.read_text().splitlines():
            parsed = parse_line(line)
            if parsed is None or str(parsed[0]) not in manifest:
                continue
            number, *axes = parsed
            if grades.setdefault(number, tuple(axes)) != tuple(axes):
                raise ValueError(f"id {number} is graded twice, differently, in {path}")
    return grades


def merge(tag: str, paths: list[Path]) -> None:
    manifest = json.loads((WORK_DIR / tag / "manifest.json").read_text())
    done = graded_pairs(JUDGMENTS_PATH) | graded_pairs(ANCHORS_PATH)
    added = Counter()
    with JUDGMENTS_PATH.open("a") as judgments, ANCHORS_PATH.open("a") as anchor_judgments:
        for number, grades in judged_lines(paths, manifest).items():
            entry = manifest[str(number)]
            if (entry["query"], entry["url"]) in done:
                added["duplicate"] += 1
                continue
            record = {"query": entry["query"], "url": entry["url"]} | dict(zip(AXES, grades))
            (anchor_judgments if entry["anchor"] else judgments).write(json.dumps(record) + "\n")
            done.add((entry["query"], entry["url"]))
            added["anchor" if entry["anchor"] else "image"] += 1
    print(f"merged {tag}: {dict(added)}; {len(manifest)} in the manifest")


def image_rows() -> pd.DataFrame:
    grades = {(row["query"], row["url"]): row for row in read_jsonl(JUDGMENTS_PATH)}
    rows = []
    for row in read_jsonl(POOLS_PATH):
        for image in row["images"]:
            grade = grades[(row["query"], image["url"])]
            rows.append(
                {"query": row["query"], "url": image["url"], "title": image["title"], "extract": image["extract"]}
                | {axis: grade[axis] for axis in AXES}
                | {"pools": IMAGE_POOL, "ss_source": ""}
            )
    return pd.DataFrame(rows)


def anchor_agreement() -> dict:
    """Per axis, how the anchors' grades beside images differ from their pass-3 grades."""
    grades = {(row["query"], row["url"]): row for row in read_jsonl(PASS3_PATH)}
    anchor_rows = read_jsonl(ANCHORS_PATH)
    agreement = {"anchors": len(anchor_rows)}
    for axis in AXES:
        differences = [row[axis] - grades[(row["query"], row["url"])][axis] for row in anchor_rows]
        agreement[axis] = {
            "mean_difference": round(sum(differences) / len(differences), 3),
            "within_one": round(sum(abs(difference) <= 1 for difference in differences) / len(differences), 3),
        }
    return agreement


def build() -> None:
    llm = pd.read_csv(LEARNING_TO_RANK_LLM_DATASET_PATH, index_col=0, lineterminator="\n")
    images = image_rows()
    dataset = pd.concat([llm, images], ignore_index=True)
    dataset.to_csv(DATASET_PATH, errors="replace")
    print(f"{len(images)} image rows on {images['query'].nunique()} queries, {len(dataset)} rows -> {DATASET_PATH}")
    for axis in AXES:
        distribution = dict(sorted(Counter(images[axis]).items()))
        print(f"  {axis}: {distribution}, mean {images[axis].mean():.2f} for images, {llm[axis].mean():.2f} for web")
    print(f"  overall >= 4: {(images['overall'] >= 4).mean():.1%} of images, {(llm['overall'] >= 4).mean():.1%} of web")
    print(f"  anchors: {anchor_agreement()}")


def main() -> None:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["overlay", "batches", "merge", "build"])
    parser.add_argument("tag", nargs="?")
    parser.add_argument("extra", nargs="*", help="batches: the number of batches; merge: the judges' output files")
    args = parser.parse_args()

    if args.command == "batches":
        batches(args.tag, int(args.extra[0]))
    elif args.command == "merge":
        merge(args.tag, [Path(path) for path in args.extra])
    else:
        commands = {"overlay": overlay, "build": build}
        commands[args.command]()


if __name__ == "__main__":
    main()
