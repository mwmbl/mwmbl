"""LTR training rows for Commons image pages: page fitting, retrieval, pass-3 batches and merging (no network)."""

import json
from hashlib import sha256

import pytest

from mwmbl.rankeval.seed_images import indexing, ltr_data
from mwmbl.tinysearchengine.indexer import Document

COMMONS = "https://commons.wikimedia.org/wiki/File:"


def web_documents(term: str, count: int) -> list[Document]:
    # Hashes in the extracts, because a page holds a fixed number of compressed bytes and repeated text packs away.
    return [
        Document(
            f"{term} page {i}",
            f"https://github.com/{term}/{i}",
            f"All about {term} " + sha256(str(i).encode()).hexdigest(),
            term=term,
        )
        for i in range(count)
    ]


def image(term: str, name: str) -> Document:
    return Document(f"{term} {name}", f"{COMMONS}{name}.jpg", f"A photo of {term}", term=term)


def test_an_image_fits_on_a_thin_page_but_loses_to_a_full_one():
    thin = ltr_data.stored_page(web_documents("clock", 2), [image("clock", "A")])
    full = ltr_data.stored_page(web_documents("clock", 200), [image("clock", "A")])

    assert any(indexing.is_image(d.url) for d in thin)
    assert not any(indexing.is_image(d.url) for d in full)


def test_a_query_reads_its_terms_and_bigrams():
    assert ltr_data.lookup_terms(["big", "ben", "clock"]) == {"big", "ben", "clock", "big ben", "ben clock"}


def test_a_query_gets_the_images_stored_under_its_terms_that_pass_the_majority_rule():
    both = image("ben", "Big_ben")
    both.title = "big ben"
    one_term = image("ben", "Ben_nevis")
    pooled = image("big", "Pooled")
    already_indexed = image("big ben", "Production_holds_it")
    wrong_term = image("other", "Wrong_term")
    pages = {
        ltr_data.page_index("ben"): [both, one_term],
        ltr_data.page_index("big"): [pooled],
        ltr_data.page_index("big ben"): [wrong_term, already_indexed],
    }
    overlaid = {document.url for document in (both, one_term, pooled, wrong_term)}

    row = ltr_data.query_images("big ben", pages, overlaid, pool_urls={pooled.url})

    assert [entry["url"] for entry in row["images"]] == [both.url]
    assert (row["retrieved"], row["already_pooled"]) == (3, 1)


@pytest.mark.parametrize(
    "line, parsed",
    [
        ("3 || 2 || 1 || 6", (3, 2, 1, 6)),
        ("3 || 4 || 1 || 6", None),
        ("3 || 2 || 1", None),
        ("note: 3 || 2 || 1 || 6", None),
    ],
)
def test_judge_lines_are_read_as_the_pass_3_merge_reads_them(line, parsed):
    assert ltr_data.parse_line(line) == parsed


@pytest.fixture
def ltr_dir(tmp_path, monkeypatch):
    llm_relabel = tmp_path / "llm_relabel"
    llm_relabel.mkdir()
    pool = {
        "query": "big ben",
        "candidates": [
            {"url": f"https://site{i}.com/", "title": f"Site {i}", "extract": "Clock tower"} for i in range(3)
        ],
    }
    (llm_relabel / "pass2_pool.jsonl").write_text(json.dumps(pool) + "\n")
    (llm_relabel / "pass1_intents.jsonl").write_text(json.dumps({"query": "big ben", "intent": "entity"}) + "\n")
    pass3 = [
        {"query": "big ben", "url": c["url"], "relevance": 3, "ethos": 2, "overall": 8} for c in pool["candidates"]
    ]
    (llm_relabel / "pass3_judgments.jsonl").write_text("".join(json.dumps(row) + "\n" for row in pass3))
    images = [{"url": f"{COMMONS}Big_ben.jpg", "title": "Big ben", "extract": "The clock tower at night"}]
    pools = {"query": "big ben", "retrieved": 1, "already_pooled": 0, "images": images}
    ltr = tmp_path / "ltr"
    ltr.mkdir()
    (ltr / "pools.jsonl").write_text(json.dumps(pools) + "\n")
    monkeypatch.setattr(ltr_data, "LLM_RELABEL_DIR", llm_relabel)
    monkeypatch.setattr(ltr_data, "PASS2_POOL_PATH", llm_relabel / "pass2_pool.jsonl")
    monkeypatch.setattr(ltr_data, "PASS3_PATH", llm_relabel / "pass3_judgments.jsonl")
    for name in ("POOLS_PATH", "JUDGMENTS_PATH", "ANCHORS_PATH"):
        monkeypatch.setattr(ltr_data, name, ltr / getattr(ltr_data, name).name)
    monkeypatch.setattr(ltr_data, "WORK_DIR", ltr / "work")
    return ltr


def test_batches_mix_a_querys_images_with_graded_anchors_blind(ltr_dir):
    ltr_data.batches("t1", 1)

    manifest = json.loads((ltr_dir / "work" / "t1" / "manifest.json").read_text())
    batch = (ltr_dir / "work" / "t1" / "batch_00.txt").read_text()
    assert batch.startswith(ltr_data.JUDGE_PROMPT)
    assert "=== INTENT: entity" in batch and "anchor" not in batch
    assert sorted(entry["anchor"] for entry in manifest.values()) == [False, True, True]


def test_merge_splits_image_grades_from_anchor_grades_and_skips_repeats(ltr_dir):
    ltr_data.batches("t1", 1)
    manifest = json.loads((ltr_dir / "work" / "t1" / "manifest.json").read_text())
    output = ltr_dir / "out.txt"
    output.write_text("".join(f"{number} || 1 || 3 || 3\n" for number in manifest) + "junk\n")

    ltr_data.merge("t1", [output, output])
    ltr_data.merge("t1", [output])

    assert [row["overall"] for row in ltr_data.read_jsonl(ltr_dir / "pass3_judgments.jsonl")] == [3]
    assert len(ltr_data.read_jsonl(ltr_dir / "pass3_anchors.jsonl")) == 2
    agreement = ltr_data.anchor_agreement()
    assert agreement["anchors"] == 2
    assert agreement["overall"] == {"mean_difference": -5.0, "within_one": 0.0}
    assert agreement["ethos"] == {"mean_difference": 1.0, "within_one": 1.0}
    rows = ltr_data.image_rows()
    assert rows[["url", "overall", "pools"]].values.tolist() == [[f"{COMMONS}Big_ben.jpg", 3, ltr_data.IMAGE_POOL]]


def test_a_second_different_grade_for_one_id_fails_the_merge(ltr_dir):
    ltr_data.batches("t1", 1)
    output = ltr_dir / "out.txt"
    output.write_text("1 || 1 || 3 || 3\n1 || 3 || 3 || 10\n")

    with pytest.raises(ValueError, match="graded twice"):
        ltr_data.merge("t1", [output])
