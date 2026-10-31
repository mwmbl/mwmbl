"""
The crawler agreement overview: AgreementRollups laid out for the admin page, with every crawler
held against the network's own spread in the window.

As in the rollups, nothing here passes judgement on a crawler. Crawlers are ranked by how far
they sit from the network's median, measured in the network's interquartile range. The funnel's
allowance is chance at a crawler's volume, measured in standard errors. Neither is a fixed
cut-off a bad actor could learn to stay under.
"""

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from mwmbl.crawler.agreement_rollups import TAIL_SHARE, AgreementRollups, CrawlerRollup

GRID_CRAWLERS = 18
"""The busiest crawlers, as many as the pair grid has room for."""

MIN_PAIR_COMPARISONS = 20
"""Below this many shared URLs a pair's median is mostly noise. It is a sample size, not a bar
for a crawler's behaviour."""

TOP_LABELS = 3
FUNNEL_STEPS = 40
FUNNEL_TOP = 4 * TAIL_SHARE

SELECTED = "#1F5FBF"
INK = "#1B1D21"
MUTED = "#4F535A"
HATCH = "repeating-linear-gradient(45deg, #ECEAE4 0 2px, #FFFFFF 2px 5px)"
HEAT_STOPS = [
    (0, (107, 34, 8)),
    (0.25, (180, 68, 26)),
    (0.5, (224, 138, 75)),
    (0.75, (242, 211, 179)),
    (1, (244, 241, 234)),
]

TREND_WIDTH = 600
TREND_HEIGHT = 240
SPARK_WIDTH = 130
SPARK_HEIGHT = 30


@dataclass(frozen=True)
class Spread:
    p25: float
    median: float
    p75: float

    @property
    def iqr(self) -> float:
        return self.p75 - self.p25


@dataclass(frozen=True)
class Column:
    attribute: str
    label: str
    higher_is_worse: bool
    is_rate: bool


COLUMNS = [
    Column("median_extract_similarity_as_submitter", "Extract sim, as submitter", False, False),
    Column("median_extract_similarity_as_stored", "Extract sim, as stored copy", False, False),
    Column("median_title_similarity", "Title similarity", False, False),
    Column("empty_extract_rate", "Empty extracts", True, True),
    Column("median_recent_score", "Agreement, gap under a day", False, False),
]


def spread(values: list[float | None]) -> Spread | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    p25, middle, p75 = np.percentile(present, [25, 50, 75])
    return Spread(float(p25), float(middle), float(p75))


def distance_from_network(value: float | None, network: Spread | None, higher_is_worse: bool) -> float | None:
    """How many of the network's interquartile ranges the value sits on the bad side of its median.
    Where the network has no spread there is no scale to measure in, so no distance."""
    if value is None or network is None or network.iqr == 0:
        return None
    away = value - network.median if higher_is_worse else network.median - value
    return away / network.iqr


def tail_z(crawler: CrawlerRollup, typical_rate: float | None) -> float | None:
    """Standard errors between its tail rate and the typical crawler's, at its own volume."""
    if crawler.tail_rate is None or typical_rate is None or typical_rate in (0, 1):
        return None
    standard_error = math.sqrt(typical_rate * (1 - typical_rate) / crawler.recent_comparisons)
    return (crawler.tail_rate - typical_rate) / standard_error


def heat_colour(similarity: float) -> str:
    for (x0, c0), (x1, c1) in zip(HEAT_STOPS, HEAT_STOPS[1:]):
        if similarity <= x1:
            t = (similarity - x0) / (x1 - x0)
            channels = [round(a + (b - a) * t) for a, b in zip(c0, c1)]
            return f"rgb({channels[0]},{channels[1]},{channels[2]})"
    return "rgb(244,241,234)"


def _clamp_percent(x: float) -> float:
    return max(0.0, min(100.0, x))


def _nice_ticks(top: float) -> list[int]:
    """1, 2, 5, 10, 20, ... up to the first at or above top."""
    ticks = [1]
    while ticks[-1] < top:
        decade = 10 ** (len(ticks) // 3)
        ticks.append((1, 2, 5)[len(ticks) % 3] * decade)
    return ticks


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)}%"


@dataclass
class _Crawler:
    rollup: CrawlerRollup
    name: str
    has_username: bool
    distances: list[float | None]
    tail_z: float | None

    @property
    def off_network(self) -> float | None:
        present = [d for d in self.distances + [self.tail_z] if d is not None]
        return max(present) if present else None


def build_panel(rollups: AgreementRollups, usernames: dict[int, str], selected_id: int | None, days: int) -> dict:
    compared = [crawler for crawler in rollups.crawlers if crawler.comparisons > 0]
    unmet = [crawler for crawler in rollups.crawlers if crawler.comparisons == 0]
    network = {column.attribute: spread([getattr(c, column.attribute) for c in compared]) for column in COLUMNS}
    typical_rate = spread([c.tail_rate for c in compared])
    p0 = typical_rate.median if typical_rate else None

    def name(user_id: int) -> str:
        return usernames.get(user_id, f"deleted user {user_id}")

    crawlers = [
        _Crawler(
            rollup=c,
            name=name(c.user_id),
            has_username=c.user_id in usernames,
            distances=[
                distance_from_network(getattr(c, col.attribute), network[col.attribute], col.higher_is_worse)
                for col in COLUMNS
            ],
            tail_z=tail_z(c, p0),
        )
        for c in compared
    ]
    crawlers.sort(key=lambda c: (c.off_network is None, -(c.off_network or 0)))
    if not crawlers:
        return {"empty": True, "unmet": _unmet(unmet, name)}

    compared_ids = [c.rollup.user_id for c in crawlers]
    selected = selected_id if selected_id in compared_ids else compared_ids[0]
    top_ids = compared_ids[:TOP_LABELS]
    trends = _daily_series(rollups)
    num_days = (rollups.end - rollups.start).days + 1

    total_comparisons = sum(pair.comparisons for pair in rollups.pairs)
    side_comparisons = sum(c.comparisons for c in compared)
    empty_sides = sum(c.comparisons * c.empty_extract_rate for c in compared)
    submitter_spread = network["median_extract_similarity_as_submitter"]
    return {
        "empty": False,
        "selected": next(c for c in crawlers if c.rollup.user_id == selected),
        "totals": {
            "comparisons": total_comparisons,
            "crawlers": len(crawlers),
            "median": _fmt(submitter_spread.median if submitter_spread else None),
            "empty_share": _percent(empty_sides / side_comparisons),
        },
        "columns": COLUMNS,
        "grid": _grid(rollups, crawlers, selected),
        "scatter": _scatter(crawlers, submitter_spread, selected, top_ids),
        "funnel": _funnel(crawlers, p0, selected),
        "rows": _rows(crawlers, network, trends, num_days, selected),
        "trend": _trend(crawlers, trends, num_days, selected, days),
        "unmet": _unmet(unmet, name),
    }


def _unmet(unmet: list[CrawlerRollup], name) -> list[dict]:
    return [{"name": name(c.user_id), "submissions": c.submissions} for c in unmet]


def _daily_series(rollups: AgreementRollups) -> dict[int, dict[int, tuple[float | None, str | None]]]:
    """Each crawler's median and version by day number in the window."""
    series = defaultdict(dict)
    for day in rollups.daily:
        series[day.user_id][(day.day - rollups.start).days] = (day.median_extract_similarity, day.crawler_version)
    return series


def _grid(rollups: AgreementRollups, crawlers: list[_Crawler], selected: int) -> dict:
    busiest = sorted(crawlers, key=lambda c: -c.rollup.comparisons)[:GRID_CRAWLERS]
    pairs = {(pair.user_id, pair.existing_user_id): pair for pair in rollups.pairs}

    def label(c: _Crawler) -> dict:
        is_selected = c.rollup.user_id == selected
        return {"name": c.name, "colour": SELECTED if is_selected else MUTED, "weight": 600 if is_selected else 400}

    def cell(row: _Crawler, column: _Crawler) -> dict:
        pair = pairs.get((row.rollup.user_id, column.rollup.user_id))
        if pair is None or pair.comparisons < MIN_PAIR_COMPARISONS:
            return {"background": HATCH, "ring": "none", "tip": f"{row.name} over {column.name}: too few shared URLs"}
        if pair.median_extract_similarity is None:
            return {"background": HATCH, "ring": "none", "tip": f"{row.name} over {column.name}: every extract empty"}
        on_cross = selected in (row.rollup.user_id, column.rollup.user_id)
        similarity = pair.median_extract_similarity
        return {
            "background": heat_colour(similarity),
            "ring": f"inset 0 0 0 1.5px {SELECTED}" if on_cross else "none",
            "tip": f"{row.name} over {column.name}: {similarity:.2f} across {pair.comparisons} URLs",
        }

    return {
        "columns": [label(c) for c in busiest],
        "rows": [label(row) | {"cells": [cell(row, column) for column in busiest]} for row in busiest],
    }


def _dot_label(c: _Crawler, x: float, y: float, selected: int, text: str | None = None) -> dict:
    is_selected = c.rollup.user_id == selected
    return {
        "name": text or c.name,
        "x": f"{x:.2f}",
        "y": f"{y:.2f}",
        "colour": SELECTED if is_selected else INK,
        "weight": 600 if is_selected else 400,
    }


def _scatter(crawlers: list[_Crawler], network: Spread | None, selected: int, top_ids: list[int]) -> dict:
    plotted = [c for c in crawlers if c.rollup.median_extract_similarity_as_submitter is not None]
    x_ticks = _nice_ticks(max([2] + [c.rollup.partners for c in plotted]))

    def x_pos(partners: int) -> float:
        return math.log(partners) / math.log(x_ticks[-1]) * 100

    most_comparisons = max([1] + [c.rollup.comparisons for c in plotted])
    dots, labels = [], []
    for c in sorted(plotted, key=lambda c: -c.rollup.comparisons):
        similarity = c.rollup.median_extract_similarity_as_submitter
        x, y = x_pos(c.rollup.partners), _clamp_percent(similarity * 100)
        size = round(8 + 16 * math.sqrt(c.rollup.comparisons / most_comparisons))
        is_selected = c.rollup.user_id == selected
        dots.append(
            {
                "user_id": c.rollup.user_id,
                "x": f"{x:.2f}",
                "y": f"{y:.2f}",
                "size": size,
                "offset": -size / 2,
                "fill": heat_colour(similarity),
                "border": f"3px solid {SELECTED}" if is_selected else "1px solid rgba(27,29,33,0.45)",
                "label": f"{c.name}: {c.rollup.partners} partners, median {similarity:.2f}",
            }
        )
        if is_selected or c.rollup.user_id in top_ids:
            labels.append(_dot_label(c, x, y, selected))
    return {
        "x_ticks": [{"pos": f"{x_pos(tick):.2f}", "label": tick} for tick in x_ticks],
        "y_ticks": [{"pos": tick * 20, "label": f"{tick / 5:.1f}"} for tick in range(6)],
        "band": {
            "bottom": f"{network.p25 * 100:.2f}",
            "height": f"{network.iqr * 100:.2f}",
            "median": f"{network.median * 100:.2f}",
        }
        if network
        else None,
        "dots": dots,
        "labels": labels,
    }


def _funnel(crawlers: list[_Crawler], p0: float | None, selected: int) -> dict | None:
    plotted = [c for c in crawlers if c.rollup.recent_comparisons > 0]
    if p0 is None or not plotted:
        return None
    volumes = [c.rollup.recent_comparisons for c in plotted]
    low = math.floor(math.log10(min(volumes)))
    high = math.floor(math.log10(max(volumes))) + 1
    steps = (1, 2, 5) if high - low <= 3 else (1,)
    x_ticks = [step * 10**decade for decade in range(low, high) for step in steps] + [10**high]

    def fx(count: float) -> float:
        return _clamp_percent((math.log10(count) - low) / (high - low) * 100)

    def fy(rate: float) -> float:
        return _clamp_percent(rate / FUNNEL_TOP * 100)

    counts = [10 ** (low + (high - low) * i / FUNNEL_STEPS) for i in range(FUNNEL_STEPS + 1)]

    def band(z: int) -> str:
        allowances = [z * math.sqrt(p0 * (1 - p0) / count) for count in counts]
        upper = [f"{fx(c):.2f},{100 - fy(p0 + a):.2f}" for c, a in zip(counts, allowances)]
        lower = [f"{fx(c):.2f},{100 - fy(max(0.0, p0 - a)):.2f}" for c, a in zip(counts, allowances)]
        return " ".join(upper + lower[::-1])

    furthest = [c.rollup.user_id for c in sorted(plotted, key=lambda c: -(c.tail_z or 0))[:TOP_LABELS]]
    dots, labels = [], []
    for c in sorted(plotted, key=lambda c: -c.rollup.recent_comparisons):
        rate = c.rollup.tail_rate
        x, y = fx(c.rollup.recent_comparisons), fy(rate)
        is_selected = c.rollup.user_id == selected
        dots.append(
            {
                "user_id": c.rollup.user_id,
                "x": f"{x:.2f}",
                "y": f"{y:.2f}",
                "fill": SELECTED if is_selected else INK,
                "border": "3px solid #FFFFFF" if is_selected else "1px solid #FFFFFF",
                "label": f"{c.name}: {_percent(rate)} of {c.rollup.recent_comparisons} comparisons in the network tail",
            }
        )
        if is_selected or c.rollup.user_id in furthest:
            off_top = rate > FUNNEL_TOP
            text = f"{c.name} ↑ {_percent(rate)}" if off_top else c.name
            label = _dot_label(c, x, y, selected, text)
            # Dots off the top all sit on its edge, so their labels go inside the plot, one under another.
            label["drop"] = 6 + 20 * sum(other["drop"] is not None for other in labels) if off_top else None
            labels.append(label)
    return {
        "outer": band(3),
        "inner": band(2),
        "p0_y": f"{100 - fy(p0):.2f}",
        "p0": f"{p0 * 100:.1f}%",
        "x_ticks": [{"pos": f"{fx(tick):.2f}", "label": f"{tick:,}"} for tick in x_ticks],
        "y_ticks": [{"pos": f"{fy(step * TAIL_SHARE):.2f}", "label": _percent(step * TAIL_SHARE)} for step in range(5)],
        "dots": dots,
        "labels": labels,
    }


def _strip(value: float | None, network: Spread | None, top: float, text: str) -> dict:
    def pos(v: float) -> str:
        return f"{_clamp_percent(v / top * 100):.2f}"

    return {
        "band_left": pos(network.p25) if network else None,
        "band_width": f"{_clamp_percent(network.iqr / top * 100):.2f}" if network else None,
        "median": pos(network.median) if network else None,
        "pos": pos(value) if value is not None else None,
        "text": text,
    }


def _spark_points(series: dict[int, float], num_days: int) -> str:
    last_day = max(num_days - 1, 1)
    return " ".join(
        f"{day / last_day * (SPARK_WIDTH - 4) + 2:.1f},{SPARK_HEIGHT - 2 - value * (SPARK_HEIGHT - 4):.1f}"
        for day, value in sorted(series.items())
    )


def _medians(trend: dict[int, tuple[float | None, str | None]]) -> dict[int, float]:
    return {day: value for day, (value, _) in trend.items() if value is not None}


def _versions(trend: dict[int, tuple[float | None, str | None]]) -> list[tuple[int, str]]:
    """The day each version was first seen. An operator running several machines can switch
    back and forth between versions, and only a version not seen before is a change."""
    first_seen = {}
    for day, (_, version) in sorted(trend.items()):
        if version is not None and version not in first_seen:
            first_seen[version] = day
    return [(day, version) for version, day in first_seen.items()]


def _network_daily(trends) -> dict[int, Spread]:
    by_day = defaultdict(list)
    for trend in trends.values():
        for day, value in _medians(trend).items():
            by_day[day].append(value)
    return {day: spread(values) for day, values in sorted(by_day.items())}


def _rows(crawlers: list[_Crawler], network: dict, trends, num_days: int, selected: int) -> list[dict]:
    rate_tops = {
        col.attribute: max([getattr(c.rollup, col.attribute) or 0 for c in crawlers]) or 1
        for col in COLUMNS
        if col.is_rate
    }
    network_spark = _spark_points({day: s.median for day, s in _network_daily(trends).items()}, num_days)
    furthest = max(c.off_network or 0 for c in crawlers)
    rows = []
    for c in crawlers:
        values = [getattr(c.rollup, col.attribute) for col in COLUMNS]
        versions = [version for _, version in _versions(trends[c.rollup.user_id])]
        rows.append(
            {
                "user_id": c.rollup.user_id,
                "name": c.name,
                "has_username": c.has_username,
                "selected": c.rollup.user_id == selected,
                "version": " → ".join([versions[0], versions[-1]] if len(versions) > 1 else versions) or "—",
                "comparisons": c.rollup.comparisons,
                "partners": c.rollup.partners,
                "checked": _percent(c.rollup.met_rate),
                "strips": [
                    _strip(
                        value,
                        network[col.attribute],
                        rate_tops.get(col.attribute, 1),
                        _percent(value) if col.is_rate else _fmt(value),
                    )
                    for col, value in zip(COLUMNS, values)
                ],
                "spark": _spark_points(_medians(trends[c.rollup.user_id]), num_days),
                "network_spark": network_spark,
                "score_width": max(2, round((c.off_network or 0) / furthest * 60)) if furthest > 0 else 2,
                "score": _fmt(c.off_network),
            }
        )
    return rows


def _trend(crawlers: list[_Crawler], trends, num_days: int, selected: int, days: int) -> dict:
    last_day = max(num_days - 1, 1)

    def tx(day: int) -> float:
        return day / last_day * TREND_WIDTH

    def ty(value: float) -> str:
        return f"{TREND_HEIGHT - value * TREND_HEIGHT:.1f}"

    network = _network_daily(trends)
    upper = [f"{tx(day):.1f},{ty(s.p75)}" for day, s in network.items()]
    lower = [f"{tx(day):.1f},{ty(s.p25)}" for day, s in network.items()]
    own = _medians(trends[selected])
    changes = _versions(trends[selected])[1:]
    name = next(c.name for c in crawlers if c.rollup.user_id == selected)
    return {
        "y_ticks": [{"pos": tick * 20, "label": f"{tick / 5:.1f}"} for tick in range(6)],
        "band": " ".join(upper + lower[::-1]),
        "network": " ".join(f"{tx(day):.1f},{ty(s.median)}" for day, s in network.items()),
        "own": " ".join(f"{tx(day):.1f},{ty(value)}" for day, value in sorted(own.items())),
        "has_own": bool(own),
        "changes": [
            {"x": f"{tx(day):.1f}", "left": f"{tx(day) / TREND_WIDTH * 100:.2f}", "label": f"version {version}"}
            for day, version in changes
        ],
        "x_labels": [f"{days - 1} days ago", f"{(days - 1) // 2} days ago", "today"],
        "label": f"{name} daily median extract similarity against the network over {days} days",
    }
