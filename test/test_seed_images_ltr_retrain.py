"""The image LTR retrain's evaluation: folds, training frames, scoring, the image cap and the overlaid index."""

import pandas as pd

from mwmbl.rankeval.seed_images import ltr_retrain
from mwmbl.tinysearchengine.indexer import Document

COMMONS = "https://commons.wikimedia.org/wiki/File:"


def llm_row(query: str, url: str, relevance: int, overall: int, pools: str = "standard") -> dict:
    return dict(
        query=query, url=url, title=url, extract=None, score=None, relevance=relevance, overall=overall, pools=pools
    )


def llm_frame() -> pd.DataFrame:
    rows = [
        llm_row("Big Ben", "https://a.org", 3, 8),
        llm_row("big ben", f"{COMMONS}Ben.jpg", 2, 6, ltr_retrain.IMAGE_POOL),
        llm_row("big ben", "https://b.org", 2, 4),
        llm_row("held out", "https://c.org", 3, 9),
    ]
    return ltr_retrain.normalised(pd.DataFrame(rows))


def curation_frame() -> pd.DataFrame:
    pairs = [("big ben", "https://a.org"), ("big ben", "https://d.org"), ("held out", "https://e.org")]
    rows = [{"query": q, "qnorm": q, "url": url, "title": "", "extract": "", "score": 0.0} for q, url in pairs]
    return pd.DataFrame(rows).assign(label=1.0, weight=2.0)


def test_every_key_gets_one_fold_and_the_folds_are_balanced():
    keys = [f"query {i}" for i in range(23)] * 2

    assigned = ltr_retrain.folds(keys)

    assert assigned == ltr_retrain.folds(list(reversed(keys)))
    sizes = pd.Series(assigned).value_counts()
    assert set(assigned) == set(keys) and sizes.max() - sizes.min() <= 1


def test_the_training_frame_leaves_out_held_out_queries_and_images_when_the_arm_has_none():
    arm = ltr_retrain.Arm(with_images=False, label="overall", threshold=6)

    frame = ltr_retrain.training_frame(llm_frame(), curation_frame(), {"held out"}, arm)

    assert frame["url"].tolist() == ["https://a.org", "https://b.org", "https://d.org"]
    assert frame["label"].tolist() == [1.0, 0.0, 1.0]
    # Curation rows count for half, as in the deployed model, and only where no LLM row grades the pair.
    assert frame["weight"].tolist() == [1.0, 1.0, 1.0]


def test_the_relevance_label_grades_the_rows_by_relevance_alone():
    arm = ltr_retrain.Arm(with_images=True, label="relevance", threshold=2)

    frame = ltr_retrain.training_frame(llm_frame(), curation_frame().iloc[:0], {"held out"}, arm)

    assert frame["url"].tolist() == ["https://a.org", f"{COMMONS}Ben.jpg", "https://b.org"]
    assert frame["label"].tolist() == [1.0, 1.0, 1.0]


def test_ndcg_skips_queries_with_one_row_or_no_gain_and_placement_counts_images():
    test = llm_frame().assign(prediction=[0.1, 0.9, 0.5, 0.3])

    scores = ltr_retrain.per_query_ndcg(test, "overall")
    placement = ltr_retrain.image_placement(test)

    assert list(scores) == ["big ben"] and 0 < scores["big ben"] < 1
    assert placement == {"image_queries": 1, "images_in_top_ten": 1.0, "image_first": 1.0}


def test_the_cap_keeps_the_first_images_and_moves_the_web_results_up():
    results = [
        Document(name, f"{COMMONS}{name}.jpg" if name.startswith("i") else f"https://{name}.org", "")
        for name in ["i1", "w1", "i2", "i3", "w2"]
    ]

    assert [d.title for d in ltr_retrain.capped(results, 1)] == ["i1", "w1", "w2"]
    assert [d.title for d in ltr_retrain.capped(results, 2)] == ["i1", "w1", "i2", "w2"]
    assert ltr_retrain.capped(results, None) == results


def test_the_overlaid_index_writes_images_into_the_remote_page_and_reads_one_term(monkeypatch):
    existing = [Document("Clock", "https://clock.org", "All about clocks", term="clock")]
    other_term = Document("Bell", "https://bell.org", "Bells", term="bell")
    image = Document("Clock tower", f"{COMMONS}Clock.jpg", "A clock tower", term="clock")
    number = ltr_retrain.page_index("clock")
    reads = []

    def remote_page(page: int) -> list[Document]:
        reads.append(page)
        return existing + [other_term]

    monkeypatch.setattr(ltr_retrain, "remote_page", remote_page)
    index = ltr_retrain.OverlaidIndex({number: [image]})

    first = index.retrieve("clock")
    second = index.retrieve("clock")

    assert {d.url for d in first} == {"https://clock.org", image.url}
    assert first == second and reads == [number]
