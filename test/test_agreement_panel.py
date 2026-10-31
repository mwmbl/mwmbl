from datetime import date, timedelta

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from mwmbl.crawler.agreement_panel import Spread, build_panel, distance_from_network, spread, tail_z
from mwmbl.crawler.agreement_rollups import AgreementRollups, CrawlerRollup, DailyRollup, PairRollup
from mwmbl.models import CrawlerAgreement

START = date(2026, 10, 1)
END = START + timedelta(days=29)


def _crawler(user_id, similarity=0.8, comparisons=400, recent=200, tail=10, empty=0.03, partners=10):
    return CrawlerRollup(
        user_id=user_id,
        comparisons=comparisons,
        submitted_comparisons=comparisons // 2,
        submissions=comparisons * 2,
        partners=partners,
        median_extract_similarity_as_submitter=similarity,
        median_extract_similarity_as_stored=similarity,
        median_title_similarity=0.93,
        empty_extract_rate=empty,
        recent_comparisons=recent,
        median_recent_score=similarity,
        recent_tail_comparisons=tail,
    )


def _pair(user_id, existing_user_id, similarity, comparisons=50):
    return PairRollup(
        user_id=user_id,
        existing_user_id=existing_user_id,
        comparisons=comparisons,
        median_score=similarity,
        median_title_similarity=0.9,
        median_extract_similarity=similarity,
        recent_comparisons=comparisons,
        median_recent_score=similarity,
    )


def _rollups(crawlers, pairs=(), daily=()):
    return AgreementRollups(
        start=START, end=END, tail_threshold=0.2, crawlers=list(crawlers), pairs=list(pairs), daily=list(daily)
    )


NAMES = {1: "kestrel", 2: "heron", 3: "dunlin", 4: "magpie", 5: "linnet"}


def _network(**outliers):
    """Five crawlers, all ordinary but for the given overrides by name."""
    ids = {name: user_id for user_id, name in NAMES.items()}
    similarities = [0.80, 0.82, 0.84, 0.81, 0.83]
    empties = [0.02, 0.03, 0.04, 0.03, 0.05]
    crawlers = {user_id: {"similarity": similarities[user_id - 1], "empty": empties[user_id - 1]} for user_id in NAMES}
    for name, overrides in outliers.items():
        crawlers[ids[name]] |= overrides
    return [_crawler(user_id, **kwargs) for user_id, kwargs in crawlers.items()]


def test_spread_ignores_missing_values():
    assert spread([None, 1.0, 2.0, 3.0]) == Spread(1.5, 2.0, 2.5)
    assert spread([None]) is None


def test_distance_is_on_the_bad_side_in_the_networks_spread():
    network = Spread(0.7, 0.8, 0.9)

    assert distance_from_network(0.5, network, higher_is_worse=False) == pytest.approx(1.5)
    assert distance_from_network(0.5, network, higher_is_worse=True) == pytest.approx(-1.5)
    assert distance_from_network(0.5, Spread(0.8, 0.8, 0.8), higher_is_worse=False) is None


def test_tail_z_narrows_with_volume():
    small = _crawler(1, recent=100, tail=10)
    large = _crawler(2, recent=10_000, tail=1000)

    assert tail_z(large, 0.05) > tail_z(small, 0.05) > 0
    assert tail_z(_crawler(3, recent=0, tail=0), 0.05) is None


def test_a_crawler_that_disagrees_with_everyone_ranks_first():
    panel = build_panel(_rollups(_network(magpie={"similarity": 0.3})), NAMES, None, 30)

    assert panel["rows"][0]["name"] == "magpie"
    assert panel["selected"].name == "magpie"


def test_a_normal_median_with_a_heavy_tail_ranks_first():
    panel = build_panel(_rollups(_network(linnet={"recent": 4000, "tail": 320})), NAMES, None, 30)

    assert panel["rows"][0]["name"] == "linnet"
    (linnet_dot,) = [dot for dot in panel["funnel"]["dots"] if dot["user_id"] == 5]
    assert float(linnet_dot["y"]) == pytest.approx(0.08 / 0.2 * 100)


def test_selection_falls_back_to_the_top_crawler():
    rollups = _rollups(_network(dunlin={"empty": 0.4}))

    assert build_panel(rollups, NAMES, 2, 30)["selected"].name == "heron"
    assert build_panel(rollups, NAMES, 99, 30)["selected"].name == "dunlin"


def test_pair_grid_hatches_pairs_with_too_few_shared_urls():
    pairs = [_pair(1, 2, 0.2), _pair(2, 1, 0.9, comparisons=3)]
    panel = build_panel(_rollups(_network(), pairs), NAMES, 1, 30)

    rows = {row["name"]: row for row in panel["grid"]["rows"]}
    columns = [column["name"] for column in panel["grid"]["columns"]]
    kestrel_over_heron = rows["kestrel"]["cells"][columns.index("heron")]
    heron_over_kestrel = rows["heron"]["cells"][columns.index("kestrel")]
    assert kestrel_over_heron["tip"] == "kestrel over heron: 0.20 across 50 URLs"
    assert "inset" in kestrel_over_heron["ring"]
    assert heron_over_kestrel["tip"] == "heron over kestrel: too few shared URLs"


def test_version_changes_are_marked_on_the_trend():
    daily = [
        DailyRollup(
            user_id=3,
            day=START + timedelta(days=day),
            comparisons=10,
            median_extract_similarity=0.8,
            crawler_version="0.9.2" if day < 12 else "0.9.4",
        )
        for day in range(30)
    ]
    panel = build_panel(_rollups(_network(), daily=daily), NAMES, 3, 30)

    (change,) = panel["trend"]["changes"]
    assert change["label"] == "version 0.9.4"
    assert float(change["x"]) == pytest.approx(12 / 29 * 600, abs=0.1)
    (dunlin,) = [row for row in panel["rows"] if row["name"] == "dunlin"]
    assert dunlin["version"] == "0.9.2 → 0.9.4"


def test_crawlers_that_met_no_one_are_listed_apart():
    unmet = _crawler(5, comparisons=0, recent=0, tail=0).model_copy(update={"submissions": 12})
    panel = build_panel(_rollups(_network()[:4] + [unmet]), NAMES, None, 30)

    assert panel["unmet"] == [{"name": "linnet", "submissions": 12}]
    assert "linnet" not in [row["name"] for row in panel["rows"]]


def test_empty_window():
    assert build_panel(_rollups([]), NAMES, None, 30) == {"empty": True, "unmet": []}


@pytest.fixture
def admin_browser(db, monkeypatch):
    monkeypatch.setattr("mwmbl.crawler.worst_agreements.get_redis", lambda: fakeredis.FakeRedis())
    admin = get_user_model().objects.create_superuser(username="admin", email="admin@example.com", password="pw")
    browser = Client()
    browser.force_login(admin)
    return browser


def test_admin_page_shows_crawlers_against_the_network(admin_browser):
    User = get_user_model()
    sneaky = User.objects.create_user(username="sneaky", email="sneaky@example.com", password="pw")
    honest = User.objects.create_user(username="honest", email="honest@example.com", password="pw")
    CrawlerAgreement.objects.bulk_create(
        CrawlerAgreement(
            url=f"https://example.com/{i}",
            user=sneaky,
            existing_user_id=honest.id,
            title_similarity=1.0,
            extract_similarity=0.3,
            extract_empty=False,
            existing_extract_empty=False,
            last_crawled_delta=60,
            crawler_version="0.9.3",
        )
        for i in range(3)
    )

    response = admin_browser.get("/admin/crawler-agreement/", {"days": "7", "crawler": str(honest.id)})

    content = response.content.decode()
    assert response.status_code == 200
    assert "Every crawler against the network" in content
    assert "/admin/worst-agreements/?username=sneaky" in content
    assert "0.9.3" in content


def test_admin_page_with_no_comparisons(admin_browser):
    response = admin_browser.get("/admin/crawler-agreement/")

    assert "No crawler's copy met another crawler's" in response.content.decode()


@pytest.mark.parametrize("params", [{"days": "8"}, {"days": "abc"}, {"crawler": "sneaky"}])
def test_admin_page_rejects_bad_parameters(admin_browser, params):
    assert admin_browser.get("/admin/crawler-agreement/", params).status_code == 400
