"""The Seed Search images safety evaluation's pure logic: benchmark tiers, rates, flips and the web pool."""

from mwmbl.rankeval.seed_images import safety


def row(tier: str, label: str | None = None, rule_flagged: bool = False) -> dict:
    return {"tier": tier, "label": label, "rule_flagged": rule_flagged}


def test_benchmark_groups_follow_the_benchmark_definitions():
    assert safety.benchmark_groups(row("unsafe", "N")) == ["art", "art_evading"]
    assert safety.benchmark_groups(row("unsafe", "N", rule_flagged=True)) == ["art"]
    assert safety.benchmark_groups(row("unsafe", "P")) == []
    assert safety.benchmark_groups(row("photo_nude", rule_flagged=True)) == ["photo"]
    assert safety.benchmark_groups(row("hard", "0")) == ["safe"]
    assert safety.benchmark_groups(row("hard", "x")) == []
    assert safety.benchmark_groups(row("safe")) == ["safe"]
    assert safety.benchmark_groups(row("medical")) == ["medical"]


def test_flagged_rates_count_scores_at_or_above_the_threshold():
    groups = [["art", "art_evading"], ["art"], ["safe"]]

    rates = safety.flagged_rates(groups, [1.0, 0.9, 1.2], 1.0)

    assert rates["art"] == (1, 2)
    assert rates["art_evading"] == (1, 1)
    assert rates["safe"] == (1, 1)
    assert rates["medical"] == (0, 0)


def test_flips_count_verdicts_that_differ_at_the_threshold():
    assert safety.flips([0.4, 1.0, 1.6, 0.0], [0.6, 1.1, 1.4, 0.1], 0.5) == 1
    assert safety.flips([0.4, 1.0, 1.6, 0.0], [0.6, 1.1, 1.4, 0.1], 1.5) == 1


def test_local_flags_name_the_layers_that_judged_nsfw():
    verdict = {"rules": "safe", "nudenet": "uncertain", "zero_shot": "nsfw"}

    assert safety.local_flags(verdict) == ["zero_shot"]


def test_web_pool_puts_staan_first_then_the_standard_top_results_once_each():
    staan = [{"url": "https://a", "title": "A", "extract": "a"}]
    standard = {"url": "https://b", "title": "B", "extract": "b", "pools": ["standard"]}
    google_only = {"url": "https://c", "title": "C", "extract": "c", "pools": ["google"]}
    duplicate = {"url": "https://a", "title": "A again", "extract": "", "pools": ["standard", "staan"]}

    pool = safety.web_pool("q", staan, {"q": [duplicate, standard, google_only]})

    assert [(document.url, document.title) for document in pool] == [("https://a", "A again"), ("https://b", "B")]
