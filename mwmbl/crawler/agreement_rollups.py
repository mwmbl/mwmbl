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
    empty_extract_rate: float
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


class AgreementRollups(BaseModel):
    start: date
    end: date
    """Inclusive."""
    tail_threshold: float | None
    """The score at the network's lowest-agreeing TAIL_SHARE of recent comparisons."""
    crawlers: list[CrawlerRollup]
    pairs: list[PairRollup]


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
        COALESCE(ABS(last_crawled_delta) < {RECENT_GAP_SECONDS}, FALSE) AS recent
    FROM {CrawlerAgreement._meta.db_table}
    WHERE created_at >= %(start)s AND created_at < %(end)s
)
"""

_TAIL_THRESHOLD_SQL = f"""
SELECT PERCENTILE_CONT({TAIL_SHARE}) WITHIN GROUP (ORDER BY score) FROM comparisons WHERE recent
"""

_CRAWLERS_SQL = f"""
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
    crawler_id,
    COUNT(*),
    COUNT(*) FILTER (WHERE submitter),
    COUNT(DISTINCT partner_id),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY extract_similarity) FILTER (WHERE submitter AND both_extracts),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY extract_similarity) FILTER (WHERE NOT submitter AND both_extracts),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY title_similarity),
    AVG(own_extract_empty::int),
    COUNT(*) FILTER (WHERE recent),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score) FILTER (WHERE recent),
    COUNT(*) FILTER (WHERE recent AND score <= ({_TAIL_THRESHOLD_SQL}))
FROM sides
GROUP BY crawler_id
"""

_PAIRS_SQL = """
SELECT
    user_id,
    existing_user_id,
    COUNT(*),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY title_similarity),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY extract_similarity) FILTER (WHERE both_extracts),
    COUNT(*) FILTER (WHERE recent),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score) FILTER (WHERE recent)
FROM comparisons
GROUP BY user_id, existing_user_id
"""


def get_agreement_rollups(client: Redis, days: int) -> AgreementRollups:
    """The rollups for the last days days up to today, from the cache or worked out now."""
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
        cursor.execute(_COMPARISONS_SQL + _CRAWLERS_SQL, window)
        crawler_rows = cursor.fetchall()
        cursor.execute(_COMPARISONS_SQL + _PAIRS_SQL, window)
        pair_rows = cursor.fetchall()

    submissions = _submissions(start, end)
    crawlers = [
        CrawlerRollup(
            user_id=row[0],
            comparisons=row[1],
            submitted_comparisons=row[2],
            submissions=submissions.get(row[0], 0),
            partners=row[3],
            median_extract_similarity_as_submitter=row[4],
            median_extract_similarity_as_stored=row[5],
            median_title_similarity=row[6],
            empty_extract_rate=row[7],
            recent_comparisons=row[8],
            median_recent_score=row[9],
            recent_tail_comparisons=row[10],
        )
        for row in crawler_rows
    ]
    pairs = [
        PairRollup(
            user_id=row[0],
            existing_user_id=row[1],
            comparisons=row[2],
            median_score=row[3],
            median_title_similarity=row[4],
            median_extract_similarity=row[5],
            recent_comparisons=row[6],
            median_recent_score=row[7],
        )
        for row in pair_rows
    ]
    return AgreementRollups(start=start, end=end, tail_threshold=tail_threshold, crawlers=crawlers, pairs=pairs)


def _submissions(start: date, end: date) -> dict[int, int]:
    rows = (
        UserStats.objects.filter(date__gte=start, date__lte=end)
        .values("user_id")
        .annotate(submissions=Sum("num_results"))
    )
    return {row["user_id"]: row["submissions"] for row in rows}
