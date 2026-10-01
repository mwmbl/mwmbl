"""The holistic side-by-side judge: batching, consolidating verdicts and the report's numbers."""

import json
import sys
from pathlib import Path

import pytest

from mwmbl.rankeval.evaluation.holistic_judge import (
    COMPARISONS_PER_BATCH,
    ETHOS_RUBRIC,
    batches,
    consolidate,
    differing_queries,
    preference_for_x,
    summarise,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.llm_relabel_pass3_judge import JUDGE_PROMPT  # noqa: E402


def results(*urls: str) -> list[dict]:
    return [{"url": url, "title": f"Title {url}", "extract": f"About {url}"} for url in urls]


def verdict(judgment_id: int, better: str, strength: str = "clear", a_bad=(), b_bad=()) -> dict:
    return {
        "id": judgment_id,
        "a_best": 1,
        "b_best": 1,
        "a_bad": [list(flag) for flag in a_bad],
        "b_bad": [list(flag) for flag in b_bad],
        "missed": "",
        "better": better,
        "strength": "none" if better == "tie" else strength,
        "reason": "relevance",
        "note": "",
    }


@pytest.fixture
def work(tmp_path):
    x = {f"q{i}": results("https://x.com/", "https://shared.com/") for i in range(30)}
    y = {f"q{i}": results("https://shared.com/", "https://y.com/") for i in range(30)}
    x["same"] = y["same"] = results("https://same.com/")
    x["only in x"] = results("https://x.com/")
    (tmp_path / "x.json").write_text(json.dumps(x))
    (tmp_path / "y.json").write_text(json.dumps(y))
    work = tmp_path / "work"
    batches(tmp_path / "x.json", tmp_path / "y.json", work)
    return work


def judge_all(work: Path, choose) -> None:
    """Write one judge output per batch, choosing a verdict for each manifest entry."""
    manifest = json.loads((work / "manifest.json").read_text())["judgments"]
    for batch in sorted(work.glob("batch_*.txt")):
        ids = [int(line.split()[2]) for line in batch.read_text().splitlines() if line.startswith("=== COMPARISON")]
        lines = [json.dumps(choose(manifest[str(i)], i)) for i in ids]
        (work / batch.name.replace("batch_", "out_")).write_text("Here are my verdicts:\n" + "\n".join(lines))


def test_the_ethos_rubric_is_pass_3s_word_for_word():
    pass3_rubric = JUDGE_PROMPT[JUDGE_PROMPT.index("AXIS 2") : JUDGE_PROMPT.index("AXIS 3")].strip()

    assert ETHOS_RUBRIC == pass3_rubric


def test_only_shared_queries_whose_top_tens_differ_are_compared():
    same_top_ten = results(*(f"https://{i}.com/" for i in range(10)))
    x = {"differ": results("https://a.com/"), "same": same_top_ten + results("https://x.com/"), "x only": []}
    y = {"differ": results("https://b.com/"), "same": same_top_ten + results("https://y.com/")}

    assert differing_queries(x, y) == ["differ"]


def test_each_comparison_is_judged_in_both_orders_in_different_batches(work):
    manifest = json.loads((work / "manifest.json").read_text())["judgments"]
    batch_of = {
        int(line.split()[2]): batch.name
        for batch in work.glob("batch_*.txt")
        for line in batch.read_text().splitlines()
        if line.startswith("=== COMPARISON")
    }

    by_comparison: dict[int, list[tuple[dict, int]]] = {}
    for judgment_id, judgment in manifest.items():
        by_comparison.setdefault(judgment["comparison"], []).append((judgment, int(judgment_id)))
    assert len(by_comparison) == 30
    for pair in by_comparison.values():
        (first, first_id), (second, second_id) = pair
        assert (first["a"], second["a"]) in (("x", "y"), ("y", "x"))
        assert batch_of[first_id] != batch_of[second_id]
    assert (
        max(sum(1 for b in batch_of.values() if b == name) for name in set(batch_of.values())) <= COMPARISONS_PER_BATCH
    )


def test_a_batch_shows_each_list_in_ranked_order(work):
    manifest = json.loads((work / "manifest.json").read_text())["judgments"]
    text = (work / "batch_00.txt").read_text()
    first_id = int(text.split("=== COMPARISON ")[1].split()[0])
    block = text.split("=== COMPARISON ")[1]
    a_list = block.split("--- RESULTS A")[1].split("--- RESULTS B")[0]

    expected_first = "https://x.com/" if manifest[str(first_id)]["a"] == "x" else "https://shared.com/"
    assert a_list.strip().startswith(f"1. Title {expected_first} — {expected_first}")


def test_a_judge_preferring_x_in_both_orders_gives_a_positive_preference(work):
    judge_all(work, lambda judgment, i: verdict(i, "A" if judgment["a"] == "x" else "B", "strong"))
    consolidate(work)

    records = [json.loads(line) for line in open(work / "judgments.jsonl")]
    summary = summarise(records)

    assert summary["comparisons"] == 30 and summary["judgments"] == 60
    assert summary["preference"] == 3
    assert summary["x_preferred"] == 1 and summary["orders_agree"] == 1
    assert summary["a_chosen"] == 0.5


def test_a_judge_that_always_picks_a_is_inconsistent_and_cancels_out(work):
    judge_all(work, lambda judgment, i: verdict(i, "A"))
    consolidate(work)

    records = [json.loads(line) for line in open(work / "judgments.jsonl")]
    summary = summarise(records)

    assert summary["preference"] == 0 and summary["orders_agree"] == 0
    assert summary["a_chosen"] == 1


def test_flags_are_counted_against_the_arm_shown_on_that_side(work):
    judge_all(work, lambda judgment, i: verdict(i, "tie", a_bad=[(2, "thin")] if judgment["a"] == "y" else []))
    consolidate(work)

    summary = summarise([json.loads(line) for line in open(work / "judgments.jsonl")])

    assert summary["y_flags"]["thin"] == 30 and summary["x_flags"]["thin"] == 0


def test_consolidate_refuses_a_missing_verdict(work):
    judge_all(work, lambda judgment, i: verdict(i, "tie"))
    first_output = sorted(work.glob("out_*.txt"))[0]
    first_output.write_text("\n".join(first_output.read_text().splitlines()[:-1]))

    with pytest.raises(AssertionError, match="unjudged"):
        consolidate(work)


def test_preference_for_x_follows_whichever_side_x_was_shown_on():
    record = {"a": "y", "b": "x", "verdict": {"better": "B", "strength": "slight"}}

    assert preference_for_x(record) == 1
    assert preference_for_x(dict(record, a="x", b="y")) == -1
    assert preference_for_x(dict(record, verdict={"better": "tie"})) == 0
