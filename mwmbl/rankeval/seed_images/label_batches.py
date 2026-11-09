"""Write the batch files Sonnet judges label as reference for the query gate and image ranking.

Three kinds, each with its own manifest mapping item ids to what they label, for `labels` to join back:
- need: would a few Commons images improve the page, for every train query;
- relevance: how good each of a query's top candidates is, from its text, for a sample stratified by Jev need;
- vision: the same from the picture too, for candidates that passed every local safety layer. Their thumbnails
  are written to gate/thumbs/ for the judge to read: a one-off, user-approved exception to keeping them in
  memory only, to delete once the labels are in.

The rubrics are worded independently of the Jev questions, so the labels are an independent reference. The #610
labels were made with an earlier wording that called the engine a UK one.
"""

import io
import json
import random

from PIL import Image

from mwmbl.imagesearch.commons import new_client
from mwmbl.imagesearch.pipeline import file_id
from mwmbl.imagesearch.pixel_classifier import fetch_thumbnail
from mwmbl.imagesearch.types import SourceImage
from mwmbl.rankeval.seed_images.collect import with_backoff
from mwmbl.rankeval.seed_images.gate_data import GATE_DIR, jev_safe_candidates
from mwmbl.rankeval.seed_images.local_verdicts import VERDICTS_PATH
from mwmbl.rankeval.seed_images.queries import load_queries, read_jsonl
from mwmbl.rankeval.seed_images.run_jev import NEED_PATH

THUMBS_DIR = GATE_DIR / "thumbs"
NEED_BATCH_SIZE = 50
RELEVANCE_BATCH_SIZE = 120
VISION_BATCH_SIZE = 50
NEED_CANDIDATES_SHOWN = 5
RELEVANCE_QUERIES = 150
RELEVANCE_STRATA = 5
RELEVANCE_MIN_CANDIDATES = 3
RELEVANCE_TOP = 12
VISION_IMAGES = 100
LABEL_DESCRIPTION_CHARS = 400
SEED = 610
ITEM_MARK = "### "

NEED_RUBRIC = """\
You are helping evaluate a web search engine. For each query below, picture its results page: the ten web \
results listed, plus a small strip of three to five pictures from Wikimedia Commons, the free media library \
behind Wikipedia. Rate how much that strip would improve the page for a typical person who typed the query.

Commons holds photos of people, places, buildings, animals, plants and objects, maps, diagrams, flags, artworks \
and historical images. It holds little for software, websites, products for sale, services or current prices. \
The titles of the top pictures a Commons search returned are listed to show what is on offer. Don't mark a \
query down just because these particular pictures are poor matches - a later step picks the pictures - but do \
give 0 when the subject is not something Commons could show.

0 - No improvement, or a distraction. The person wants to reach a site, use a tool, buy something, find opening \
hours or read text, and a picture adds nothing. Examples: "facebook login", "bbc iplayer", "python list \
comprehension", "council tax bands", "cheap flights to spain", "bedrock meaning".
1 - A minor extra. A picture would not hurt and might be mildly nice, but it is not why anyone searches this. \
Examples: "premier league table", "how to grow tomatoes", "blinds", "arsenal transfer news".
2 - A clear improvement. Many people searching this would like to see the subject: a well-known person, a \
place, building, landmark, species, artwork, vehicle or historical event. Examples: "anna friel", "tower \
bridge", "red kite", "battle of hastings".
3 - Central. The query is largely about what something looks like, or a picture answers it better than text \
would. Examples: "what does a hornet look like", "map of scotland", "the starry night", "union jack", \
"tudor architecture".

Also give a short reason, a phrase of a few words such as "navigational: wants the site" or "famous \
landmark"."""

RELEVANCE_RUBRIC = """\
You are helping evaluate a web search engine that may show a few pictures from Wikimedia Commons alongside \
its web results. For each picture below, rate how good it would be as one of those pictures on the results page \
for its query. You see each picture's Commons file name, the start of its description and its categories. The \
first web results for the query are shown to tell you which meaning of an ambiguous query is meant.

0 - Wrong. A different subject, the wrong meaning of the query, or nothing anyone searching this would want. \
Example: for "jaguar" (the car), a photo of the cat; for "anna friel", a photo of a different actress.
1 - Weak. Only loosely connected: a minor aspect of the subject, a scanned document, a logo or sign, a crowd or \
event shot where the subject may be hard to find, a generic picture that would fit many queries. Example: for \
"tower bridge", a plaque on the bridge; for "red kite", a nature reserve's car park.
2 - Good. Clearly on topic and useful, but not the first picture you would choose: a detail, an unusual angle, \
an old photo, a related map or diagram. Example: for "tower bridge", its engine room; for "anna friel", a \
photo of her in a group at a premiere.
3 - Ideal. Clearly shows exactly what the query is about, and is the kind of picture a searcher would most \
want. Example: for "tower bridge", the bridge seen from the river; for "red kite", the bird in flight; for \
"the starry night", the painting itself."""

VISION_RUBRIC = (
    RELEVANCE_RUBRIC
    + """

Each item also gives the path of the picture's thumbnail. Open it with the Read tool and judge the picture \
itself as well as its text. When the two disagree, judge what the picture shows."""
)

OUTPUT_INSTRUCTIONS = """\

## Output

Use the Write tool to write {out_path} with exactly {count} lines, one per item, in any order. Each line is one \
JSON object and nothing else: no code fences, no commentary, no blank lines. Use the item ids exactly as given.
The format of a line is:
{example}
where {field} is an integer, 0, 1, 2 or 3.

## Items
"""


def write_batch(name: str, rubric: str, field: str, example: dict, items: list[str]) -> None:
    """Items to label start with ITEM_MARK and their id; any other item is context, such as a query's header."""
    # Resolved, so a judge is pointed at the real checkout's data, not a worktree's link to it.
    out_path = (GATE_DIR / f"out_{name}.txt").resolve()
    count = sum(item.startswith(ITEM_MARK) for item in items)
    instructions = OUTPUT_INSTRUCTIONS.format(out_path=out_path, count=count, example=json.dumps(example), field=field)
    (GATE_DIR / f"batch_{name}.txt").write_text(rubric + instructions + "\n" + "\n\n".join(items) + "\n")
    print(f"batch_{name}.txt: {count} items")


def write_manifest(kind: str, rows: list[dict]) -> None:
    (GATE_DIR / f"manifest_{kind}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def chunks(items: list, size: int) -> list[list]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def label_text(image: SourceImage) -> str:
    description = " ".join(image.description.split())[:LABEL_DESCRIPTION_CHARS]
    return f"File name: {image.title}\nDescription: {description}\nCategories: {' | '.join(image.categories)}"


def stratified_sample(scores: dict[str, float], strata: int, total: int, rng: random.Random) -> list[str]:
    """`total` keys, an equal share from each of `strata` equal-sized bands of their score."""
    if len(scores) < total:
        # Every band then holds at least its share, so `rng.sample` can't run short.
        raise ValueError(f"Can't sample {total} keys from {len(scores)}")
    ranked = sorted(scores, key=lambda key: (scores[key], key))
    bands = [ranked[band * len(ranked) // strata : (band + 1) * len(ranked) // strata] for band in range(strata)]
    return [key for band in bands for key in rng.sample(band, total // strata)]


def need_batches(queries: dict[str, dict], safe: dict[str, list[SourceImage]]) -> None:
    train = sorted(query for query, entry in queries.items() if entry["set"] == "train")
    manifest = [{"id": f"N{index:04d}", "query": query} for index, query in enumerate(train)]
    items = []
    for row in manifest:
        web = "\n".join(
            f"  {position}. {result['title']} - {result['url']}"
            for position, result in enumerate(queries[row["query"]]["web"], start=1)
        )
        offered = [image.title for image in safe.get(row["query"], [])[:NEED_CANDIDATES_SHOWN]]
        commons = " | ".join(offered) if offered else "(Commons returned nothing)"
        items.append(f"### {row['id']}\nQuery: {row['query']}\nWeb results:\n{web}\nCommons pictures: {commons}")
    write_manifest("need", manifest)
    example = {"id": "N0000", "need": 2, "reason": "famous landmark"}
    for number, batch in enumerate(chunks(items, NEED_BATCH_SIZE), start=1):
        write_batch(f"need_{number:02d}", NEED_RUBRIC, "need", example, batch)


def query_header(query: str, entry: dict) -> str:
    first_titles = " | ".join(result["title"] for result in entry["web"][:3])
    return f"## Query: {query}\nFirst web results: {first_titles}"


def relevance_batches(queries: dict[str, dict], safe: dict[str, list[SourceImage]], rng: random.Random) -> list:
    need = {row["query"]: row["score"] for row in read_jsonl(NEED_PATH)}
    eligible = {
        query: need[query]
        for query, entry in queries.items()
        if entry["set"] == "train" and len(safe.get(query, [])) >= RELEVANCE_MIN_CANDIDATES
    }
    sample = stratified_sample(eligible, RELEVANCE_STRATA, RELEVANCE_QUERIES, rng)
    manifest, groups = [], []
    for query in sample:
        # Shown shuffled, so the judge cannot pick up Commons' order.
        top = list(enumerate(safe[query][:RELEVANCE_TOP], start=1))
        rng.shuffle(top)
        items = [query_header(query, queries[query])]
        for commons_rank, image in top:
            row = {
                "id": f"R{len(manifest):05d}",
                "query": query,
                "file_id": file_id(image),
                "commons_rank": commons_rank,
            }
            manifest.append(row | {"image": image})
            items.append(f"### {row['id']}\n{label_text(image)}")
        groups.append(items)
    write_manifest("relevance", [{key: value for key, value in row.items() if key != "image"} for row in manifest])
    # A query's pictures stay in one batch, so its judge sees them side by side.
    batches, batch_images = [[]], 0
    for items in groups:
        group_images = len(items) - 1
        if batch_images + group_images > RELEVANCE_BATCH_SIZE:
            batches.append([])
            batch_images = 0
        batches[-1].extend(items)
        batch_images += group_images
    for number, batch in enumerate(batches, start=1):
        write_batch(f"rel_{number:02d}", RELEVANCE_RUBRIC, "relevance", {"id": "R00000", "relevance": 1}, batch)
    return manifest


def passed_every_layer(verdict: dict) -> bool:
    return all(verdict[layer] == "safe" for layer in ("rules", "nudenet", "zero_shot"))


def save_thumbnail(data: bytes, image_id: str) -> str | None:
    try:
        picture = Image.open(io.BytesIO(data)).convert("RGB")
    except OSError:
        return None
    path = THUMBS_DIR / f"{image_id}.jpg"
    picture.save(path, "JPEG")
    return str(path.resolve())


def vision_batches(relevance_manifest: list[dict], queries: dict[str, dict], rng: random.Random) -> None:
    verdicts = {row["file_id"]: row for row in read_jsonl(VERDICTS_PATH)}
    passed = [row for row in relevance_manifest if passed_every_layer(verdicts[row["file_id"]])]
    rng.shuffle(passed)
    THUMBS_DIR.mkdir(exist_ok=True)
    manifest, items = [], []
    with new_client() as client:
        for row in passed:
            if len(manifest) == VISION_IMAGES:
                break
            data, _ = with_backoff(lambda: fetch_thumbnail(client, row["image"].thumbnail_url))
            vision_id = "V" + row["id"][1:]
            thumb = data and save_thumbnail(data, vision_id)
            if not thumb:
                continue
            manifest.append({"id": vision_id, "query": row["query"], "file_id": row["file_id"], "thumb": thumb})
            header = query_header(row["query"], queries[row["query"]]).replace("## Query", "Query")
            items.append(f"### {vision_id}\n{header}\nThumbnail: {thumb}\n{label_text(row['image'])}")
    write_manifest("vision", manifest)
    for number, batch in enumerate(chunks(items, VISION_BATCH_SIZE), start=1):
        write_batch(f"vision_{number}", VISION_RUBRIC, "relevance", {"id": "V00000", "relevance": 3}, batch)


def main() -> None:
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    queries = load_queries()
    safe = jev_safe_candidates()
    need_batches(queries, safe)
    relevance_manifest = relevance_batches(queries, safe, rng)
    vision_batches(relevance_manifest, queries, rng)


if __name__ == "__main__":
    main()
