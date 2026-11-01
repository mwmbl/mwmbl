"""
Each crawler's agreement with the rest of the network, rolled up from CrawlerAgreement over a
window of days, for an admin to see at a glance which crawlers stand apart.

Nothing here judges a crawler. There is no cut-off in code for a bad actor to stay under: the
figures are for holding each crawler against the network's own spread at view time. The one
reference point worked out here is the network's lowest-agreeing tail, and that is a share of
whatever the network did in the window, not a fixed similarity.

A low similarity says two copies differ, not which one is wrong, so every comparison counts
for both crawlers: as the submitter for one and as the stored copy for the other. A crawler
that differs from many partners in both roles is the common factor.
"""

from datetime import date, datetime, time, timedelta, timezone

from django.db import connection
from django.db.models import Sum
from pydantic import BaseModel
from redis import Redis

from mwmbl.models import CrawlerAgreement, UserStats
from mwmbl.utils import utc_today

ROLLUPS_KEY = "crawler:agreement-rollups:{days}:{end}"

# Rolling these up scans every comparison in the window, so a view keeps them this long.
ROLLUPS_EXPIRE_SECONDS = 60 * 60

# Pages change, so copies crawled far apart disagree for honest reasons. The figures that
# compare crawlers' behaviour are kept to copies crawled within this of each other.
RECENT_GAP_SECONDS = 60 * 60 * 24

TAIL_SHARE = 0.05
"""The network's lowest-agreeing share of recent comparisons. Chance puts about this share of
a typical crawler's comparisons there; a crawler that injects spam into a few pages keeps a
normal median but lands there more often."""


class CrawlerRollup(BaseModel):
    user_id: int
    comparisons: int
    submitted_comparisons: int
    """Comparisons where its submission met another crawler's stored copy."""
    submissions: int
    """Everything it submitted in the window, whether or not another crawler had the URL."""
    partners: int
    median_extract_similarity_as_submitter: float | None
    median_extract_similarity_as_stored: float | None
    median_title_similarity: float | None
    empty_extract_rate: float | None
    """Share of its copies in its comparisons with an empty extract."""
    recent_comparisons: int
    median_recent_score: float | None
    recent_tail_comparisons: int

    @property
    def tail_rate(self) -> float | None:
        return self.recent_tail_comparisons / self.recent_comparisons if self.recent_comparisons else None

    @property
    def met_rate(self) -> float | None:
        """Share of its submissions that met another crawler's copy at all."""
        return self.submitted_comparisons / self.submissions if self.submissions else None


class PairRollup(BaseModel):
    user_id: int
    """The submitter."""
    existing_user_id: int
    """The crawler whose stored copy the submission met."""
    comparisons: int
    median_score: float
    median_title_similarity: float
    median_extract_similarity: float | None
    recent_comparisons: int
    median_recent_score: float | None


class DailyRollup(BaseModel):
    """A crawler's comparisons as submitter on one day, for following it over the window."""

    user_id: int
    day: date
    comparisons: int
    median_extract_similarity: float | None
    crawler_version: str | None
    """Of its last submission that day that gave one, so that a step in the median can be put against a release."""


class AgreementRollups(BaseModel):
    start: date
    end: date
    """Inclusive."""
    tail_threshold: float | None
    """The score at the network's lowest-agreeing TAIL_SHARE of recent comparisons."""
    crawlers: list[CrawlerRollup]
    pairs: list[PairRollup]
    daily: list[DailyRollup]


# The lower of title and extract similarity, or the title alone when either extract is empty,
# as worst_agreements.agreement_score ranks them.
_COMPARISONS_SQL = f"""
WITH comparisons AS (
    SELECT
        user_id,
        existing_user_id,
        title_similarity,
        extract_similarity,
        extract_empty,
        existing_extract_empty,
        NOT (extract_empty OR existing_extract_empty) AS both_extracts,
        CASE WHEN extract_empty OR existing_extract_empty THEN title_similarity
             ELSE LEAST(title_similarity, extract_similarity) END AS score,
        COALESCE(ABS(last_crawled_delta) < {RECENT_GAP_SECONDS}, FALSE) AS recent,
        crawler_version,
        created_at
    FROM {CrawlerAgreement._meta.db_table}
    WHERE created_at >= %(start)s AND created_at < %(end)s
)
"""

_TAIL_THRESHOLD_SQL = f"""
SELECT PERCENTILE_CONT({TAIL_SHARE}) WITHIN GROUP (ORDER BY score) FROM comparisons WHERE recent
"""

_CRAWLERS_SQL = """
, sides AS (
    SELECT user_id AS crawler_id, existing_user_id AS partner_id, TRUE AS submitter,
           extract_empty AS own_extract_empty, title_similarity, extract_similarity, both_extracts, score, recent
    FROM comparisons
    UNION ALL
    SELECT existing_user_id, user_id, FALSE,
           existing_extract_empty, title_similarity, extract_similarity, both_extracts, score, recent
    FROM comparisons
)
SELECT
    crawler_id AS user_id,
    COUNT(*) AS comparisons,
    COUNT(*) FILTER (WHERE submitter) AS submitted_comparisons,
    COUNT(DISTINCT partner_id) AS partners,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY extract_similarity) FILTER (WHERE submitter AND both_extracts)
        AS median_extract_similarity_as_submitter,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY extract_similarity) FILTER (WHERE NOT submitter AND both_extracts)
        AS median_extract_similarity_as_stored,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY title_similarity) AS median_title_similarity,
    AVG(own_extract_empty::int) AS empty_extract_rate,
    COUNT(*) FILTER (WHERE recent) AS recent_comparisons,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score) FILTER (WHERE recent) AS median_recent_score,
    COUNT(*) FILTER (WHERE recent AND score <= %(tail_threshold)s) AS recent_tail_comparisons
FROM sides
GROUP BY crawler_id
"""

_PAIRS_SQL = """
SELECT
    user_id,
    existing_user_id,
    COUNT(*) AS comparisons,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score) AS median_score,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY title_similarity) AS median_title_similarity,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY extract_similarity) FILTER (WHERE both_extracts)
        AS median_extract_similarity,
    COUNT(*) FILTER (WHERE recent) AS recent_comparisons,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score) FILTER (WHERE recent) AS median_recent_score
FROM comparisons
GROUP BY user_id, existing_user_id
"""


_DAILY_SQL = """
SELECT
    user_id,
    (created_at AT TIME ZONE 'UTC')::date AS day,
    COUNT(*) AS comparisons,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY extract_similarity) FILTER (WHERE both_extracts)
        AS median_extract_similarity,
    (ARRAY_AGG(crawler_version ORDER BY created_at DESC) FILTER (WHERE crawler_version IS NOT NULL))[1]
        AS crawler_version
FROM comparisons
GROUP BY user_id, day
"""


def get_agreement_rollups(client: Redis, days: int) -> AgreementRollups:
    """The rollups over the last `days` days, today included, from the cache or worked out now."""
    end = utc_today()
    key = ROLLUPS_KEY.format(days=days, end=end.isoformat())
    cached = client.get(key)
    if cached is not None:
        return AgreementRollups.model_validate_json(cached)
    rollups = compute_agreement_rollups(end - timedelta(days=days - 1), end)
    client.set(key, rollups.model_dump_json(), ex=ROLLUPS_EXPIRE_SECONDS)
    return rollups


def compute_agreement_rollups(start: date, end: date) -> AgreementRollups:
    window = {
        "start": datetime.combine(start, time(), tzinfo=timezone.utc),
        "end": datetime.combine(end + timedelta(days=1), time(), tzinfo=timezone.utc),
    }
    with connection.cursor() as cursor:
        cursor.execute(_COMPARISONS_SQL + _TAIL_THRESHOLD_SQL, window)
        (tail_threshold,) = cursor.fetchone()
        # Counted against the threshold just worked out, not a fresh one: comparisons keep
        # arriving between the queries.
        cursor.execute(_COMPARISONS_SQL + _CRAWLERS_SQL, {**window, "tail_threshold": tail_threshold})
        crawler_rows = _named_rows(cursor)
        cursor.execute(_COMPARISONS_SQL + _PAIRS_SQL, window)
        pair_rows = _named_rows(cursor)
        cursor.execute(_COMPARISONS_SQL + _DAILY_SQL, window)
        daily_rows = _named_rows(cursor)

    submissions = _submissions(start, end)
    compared_crawlers = [CrawlerRollup(**row, submissions=submissions.get(row["user_id"], 0)) for row in crawler_rows]
    # A crawler whose submissions never met another crawler's copy stands furthest apart of all.
    compared_ids = {crawler.user_id for crawler in compared_crawlers}
    unmet_crawlers = [
        _unmet_rollup(user_id, user_submissions)
        for user_id, user_submissions in submissions.items()
        if user_id not in compared_ids
    ]
    pairs = [PairRollup(**row) for row in pair_rows]
    return AgreementRollups(
        start=start,
        end=end,
        tail_threshold=tail_threshold,
        crawlers=compared_crawlers + unmet_crawlers,
        pairs=pairs,
        daily=[DailyRollup(**row) for row in daily_rows],
    )


def _named_rows(cursor) -> list[dict]:
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _unmet_rollup(user_id: int, submissions: int) -> CrawlerRollup:
    return CrawlerRollup(
        user_id=user_id,
        comparisons=0,
        submitted_comparisons=0,
        submissions=submissions,
        partners=0,
        median_extract_similarity_as_submitter=None,
        median_extract_similarity_as_stored=None,
        median_title_similarity=None,
        empty_extract_rate=None,
        recent_comparisons=0,
        median_recent_score=None,
        recent_tail_comparisons=0,
    )


def _submissions(start: date, end: date) -> dict[int, int]:
    rows = (
        UserStats.objects.filter(date__gte=start, date__lte=end)
        .values("user_id")
        .annotate(submissions=Sum("num_results"))
    )
    return {row["user_id"]: row["submissions"] for row in rows}
