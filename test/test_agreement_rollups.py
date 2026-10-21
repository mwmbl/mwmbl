from datetime import timedelta

import fakeredis
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from mwmbl.crawler.agreement_rollups import compute_agreement_rollups, get_agreement_rollups
from mwmbl.models import CrawlerAgreement, UserStats
from mwmbl.utils import utc_today

HOUR = 60 * 60


@pytest.fixture
def crawlers(db):
    return [
        get_user_model().objects.create_user(username=name, email=f"{name}@example.com", password="pw")
        for name in ["magpie", "wren", "plover"]
    ]


def _agreement(
    user, existing_user, title=1.0, extract=1.0, extract_empty=False, existing_extract_empty=False, gap=HOUR
):
    return CrawlerAgreement(
        url=f"https://example.com/{user.id}/{existing_user.id}/{title}/{extract}/{gap}",
        user=user,
        existing_user_id=existing_user.id,
        title_similarity=title,
        extract_similarity=extract,
        extract_empty=extract_empty,
        existing_extract_empty=existing_extract_empty,
        last_crawled_delta=gap,
    )


def _crawler(rollups, user):
    return next(crawler for crawler in rollups.crawlers if crawler.user_id == user.id)


def _today_rollups():
    today = utc_today()
    return compute_agreement_rollups(today, today)


def test_each_comparison_counts_for_both_crawlers(crawlers):
    magpie, wren, plover = crawlers
    CrawlerAgreement.objects.bulk_create(
        [
            _agreement(magpie, wren, extract=0.2),
            _agreement(plover, magpie, extract=0.4),
            _agreement(wren, plover, extract=0.9),
        ]
    )

    rollups = _today_rollups()

    magpie_rollup = _crawler(rollups, magpie)
    assert magpie_rollup.comparisons == 2
    assert magpie_rollup.submitted_comparisons == 1
    assert magpie_rollup.partners == 2
    assert magpie_rollup.median_extract_similarity_as_submitter == pytest.approx(0.2)
    assert magpie_rollup.median_extract_similarity_as_stored == pytest.approx(0.4)
    assert magpie_rollup.median_recent_score == pytest.approx(0.3)


def test_empty_extracts_are_counted_for_the_copy_that_has_them(crawlers):
    magpie, wren, _ = crawlers
    CrawlerAgreement.objects.bulk_create(
        [
            _agreement(magpie, wren, title=0.8, extract=0.0, extract_empty=True),
            _agreement(magpie, wren, title=0.8, extract=0.5),
        ]
    )

    rollups = _today_rollups()

    magpie_rollup = _crawler(rollups, magpie)
    assert magpie_rollup.empty_extract_rate == pytest.approx(0.5)
    assert _crawler(rollups, wren).empty_extract_rate == 0.0
    # The empty extract is left out of the extract median, and its score is the title's.
    assert magpie_rollup.median_extract_similarity_as_submitter == pytest.approx(0.5)
    assert magpie_rollup.median_recent_score == pytest.approx(0.65)


def test_tail_rate_is_against_the_networks_own_lowest_recent_scores(crawlers):
    magpie, wren, plover = crawlers
    agreements = [_agreement(wren, plover, extract=0.9) for _ in range(18)]
    agreements += [_agreement(magpie, plover, extract=0.1), _agreement(magpie, wren, extract=0.1)]
    # Crawled far apart, so these neither set the tail nor count towards it.
    agreements += [_agreement(magpie, wren, extract=0.0, gap=-30 * 24 * HOUR) for _ in range(5)]
    CrawlerAgreement.objects.bulk_create(agreements)

    rollups = _today_rollups()

    assert rollups.tail_threshold == pytest.approx(0.1)
    magpie_rollup = _crawler(rollups, magpie)
    assert magpie_rollup.recent_comparisons == 2
    assert magpie_rollup.recent_tail_comparisons == 2
    assert magpie_rollup.tail_rate == 1.0
    assert _crawler(rollups, plover).recent_tail_comparisons == 1
    assert _crawler(rollups, wren).tail_rate == pytest.approx(1 / 19)


def test_unknown_crawl_gaps_are_not_recent(crawlers):
    magpie, wren, _ = crawlers
    CrawlerAgreement.objects.bulk_create([_agreement(magpie, wren, gap=None)])

    rollups = _today_rollups()

    assert _crawler(rollups, magpie).recent_comparisons == 0
    assert _crawler(rollups, magpie).median_recent_score is None
    assert rollups.tail_threshold is None


def test_pairs_keep_the_direction(crawlers):
    magpie, wren, _ = crawlers
    CrawlerAgreement.objects.bulk_create(
        [
            _agreement(magpie, wren, title=0.9, extract=0.3),
            _agreement(magpie, wren, title=0.9, extract=0.5),
            _agreement(wren, magpie, title=1.0, extract=1.0),
        ]
    )

    pairs = {(pair.user_id, pair.existing_user_id): pair for pair in _today_rollups().pairs}

    magpie_over_wren = pairs[(magpie.id, wren.id)]
    assert magpie_over_wren.comparisons == 2
    assert magpie_over_wren.median_score == pytest.approx(0.4)
    assert magpie_over_wren.median_title_similarity == pytest.approx(0.9)
    assert magpie_over_wren.recent_comparisons == 2
    assert pairs[(wren.id, magpie.id)].median_score == 1.0


def test_met_rate_is_against_all_submissions_in_the_window(crawlers):
    magpie, wren, _ = crawlers
    today = utc_today()
    UserStats.objects.create(user=magpie, date=today, num_results=6)
    UserStats.objects.create(user=magpie, date=today - timedelta(days=1), num_results=100)
    CrawlerAgreement.objects.bulk_create([_agreement(magpie, wren), _agreement(magpie, wren, extract=0.5)])

    rollups = _today_rollups()

    assert _crawler(rollups, magpie).submissions == 6
    assert _crawler(rollups, magpie).met_rate == pytest.approx(1 / 3)
    assert _crawler(rollups, wren).met_rate is None


def test_window_leaves_out_older_comparisons(crawlers):
    magpie, wren, plover = crawlers
    CrawlerAgreement.objects.bulk_create([_agreement(magpie, wren), _agreement(plover, wren)])
    CrawlerAgreement.objects.filter(user=plover).update(created_at=timezone.now() - timedelta(days=2))

    rollups = _today_rollups()

    assert {crawler.user_id for crawler in rollups.crawlers} == {magpie.id, wren.id}


def test_get_agreement_rollups_caches_them(crawlers):
    magpie, wren, _ = crawlers
    client = fakeredis.FakeRedis()
    CrawlerAgreement.objects.bulk_create([_agreement(magpie, wren)])

    first = get_agreement_rollups(client, 7)
    CrawlerAgreement.objects.bulk_create([_agreement(wren, magpie)])
    second = get_agreement_rollups(client, 7)

    assert second == first
    assert second.start == utc_today() - timedelta(days=6)
    assert _crawler(second, magpie).comparisons == 1
