"""Export user relevance judgments (curations and votes) for judge training.

Dumps three gzipped JSONL files plus a stats summary to an output directory:

- ``curations.jsonl.gz``     one record per Curation (current interface): the
  query, the results the user saw, the results after their edits, and any
  moderation flags. Diffing original vs new results downstream yields
  preference pairs.
- ``user_curations.jsonl.gz`` one record per UserCuration action event (old
  interface): move/delete/add/validate with the result list at that point.
- ``votes.jsonl.gz``         one record per SearchResultVote (query, url, ±).
- ``domains.jsonl.gz``       one record per DomainSubmission: a human label at
  domain granularity (REJECTED/SPAM = junk domain, APPROVED = acceptable).
- ``stats.json``             counts and distributions, also printed, so a
  ``--stats-only`` run works as a data health check before exporting.

Sanitization: user accounts are replaced by opaque per-export indices (stable
across the three files so per-user noise filtering still works downstream), and
result documents keep only url/title/extract/score/state — ``user_ids`` and
``term`` are dropped.
"""

import gzip
import json
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date

from mwmbl.models import Curation, DomainSubmission, FlagCuration, SearchResultVote, UserCuration

DOCUMENT_FIELDS = ("url", "title", "extract", "score", "state")


def sanitize_results(results) -> list[dict]:
    """Keep only the document fields safe and useful for training."""
    if not isinstance(results, list):
        return []
    return [{field: doc.get(field) for field in DOCUMENT_FIELDS} for doc in results if isinstance(doc, dict)]


def query_from_results_url(url: str) -> str | None:
    """Extract the query from an old-interface results-page URL (?q=...)."""
    try:
        params = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    values = params.get("q")
    return values[0] if values else None


class Command(BaseCommand):
    help = "Export curations and votes as sanitized JSONL for training a relevance judge"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default="devdata/judgments",
            help="Directory to write the export files to (created if missing)",
        )
        parser.add_argument(
            "--since",
            type=parse_date,
            default=None,
            metavar="YYYY-MM-DD",
            help="Only export records with timestamp on or after this date",
        )
        parser.add_argument(
            "--stats-only", action="store_true", help="Only compute and print the stats summary; write no data files"
        )

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"])
        since = options["since"]
        stats_only = options["stats_only"]

        anon = AnonymousUserIds()
        stats = {}

        writers = {}
        if not stats_only:
            output_dir.mkdir(parents=True, exist_ok=True)
            for name in ("curations", "user_curations", "votes", "domains"):
                writers[name] = gzip.open(output_dir / f"{name}.jsonl.gz", "wt")

        try:
            stats["curations"] = self.export_curations(writers.get("curations"), anon, since)
            stats["user_curations"] = self.export_user_curations(writers.get("user_curations"), anon, since)
            stats["votes"] = self.export_votes(writers.get("votes"), anon, since)
            stats["domains"] = self.export_domains(writers.get("domains"), anon, since)
        finally:
            for writer in writers.values():
                writer.close()

        stats["users"] = anon.stats()
        self.stdout.write(json.dumps(stats, indent=2, default=str))
        if not stats_only:
            (output_dir / "stats.json").write_text(json.dumps(stats, indent=2, default=str))
            self.stdout.write(self.style.SUCCESS(f"Export written to {output_dir}"))

    def export_curations(self, writer, anon, since):
        queryset = Curation.objects.order_by("id")
        if since:
            queryset = queryset.filter(timestamp__date__gte=since)

        flags_by_curation = {}
        flag_queryset = FlagCuration.objects.all()
        if since:
            flag_queryset = flag_queryset.filter(curation__timestamp__date__gte=since)
        for flag in flag_queryset:
            flags_by_curation.setdefault(flag.curation_id, []).append({"flag": flag.flag, "status": flag.status})

        count = 0
        no_ops = 0
        num_changes = Counter()
        results_lengths = []
        queries = set()
        query_counts = Counter()
        timestamps = MinMax()
        flag_status_counts = Counter()
        flag_type_counts = Counter()

        for curation in queryset.iterator():
            original = sanitize_results(curation.original_results)
            new = sanitize_results(curation.new_results)
            flags = flags_by_curation.get(curation.id, [])
            record = {
                "id": curation.id,
                "user": anon.get(curation.user_id),
                "timestamp": curation.timestamp.isoformat() if curation.timestamp else None,
                "query": curation.query,
                "original_results": original,
                "new_results": new,
                "num_changes": curation.num_changes,
                "flags": flags,
            }
            if writer:
                writer.write(json.dumps(record) + "\n")

            count += 1
            if [d["url"] for d in original] == [d["url"] for d in new]:
                no_ops += 1
            num_changes[curation.num_changes] += 1
            results_lengths.append(len(original))
            queries.add(curation.query)
            query_counts[curation.query] += 1
            timestamps.add(curation.timestamp)
            for flag in flags:
                flag_status_counts[flag["status"]] += 1
                flag_type_counts[flag["flag"]] += 1

        return {
            "count": count,
            "distinct_queries": len(queries),
            "queries_curated_more_than_once": sum(1 for c in query_counts.values() if c > 1),
            "no_op_curations": no_ops,
            "num_changes_distribution": dict(sorted(num_changes.items())[:20]),
            "mean_original_results_length": round(sum(results_lengths) / len(results_lengths), 1)
            if results_lengths
            else None,
            "timestamp_range": timestamps.range(),
            "flag_status_counts": dict(flag_status_counts),
            "flag_type_counts": dict(flag_type_counts),
        }

    def export_user_curations(self, writer, anon, since):
        queryset = UserCuration.objects.order_by("id")
        if since:
            queryset = queryset.filter(timestamp__date__gte=since)

        count = 0
        unparseable_queries = 0
        type_counts = Counter()
        queries = set()
        timestamps = MinMax()

        for event in queryset.iterator():
            query = query_from_results_url(event.url)
            record = {
                "id": event.id,
                "user": anon.get(event.user_id),
                "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                "query": query,
                "curation_type": event.curation_type,
                "curation": event.curation if isinstance(event.curation, dict) else {},
                "results": sanitize_results(event.results),
            }
            if writer:
                writer.write(json.dumps(record) + "\n")

            count += 1
            if query is None:
                unparseable_queries += 1
            else:
                queries.add(query)
            type_counts[event.curation_type] += 1
            timestamps.add(event.timestamp)

        return {
            "count": count,
            "distinct_queries": len(queries),
            "unparseable_queries": unparseable_queries,
            "curation_type_counts": dict(type_counts),
            "timestamp_range": timestamps.range(),
        }

    def export_votes(self, writer, anon, since):
        queryset = SearchResultVote.objects.order_by("id")
        if since:
            queryset = queryset.filter(timestamp__date__gte=since)

        count = 0
        vote_type_counts = Counter()
        queries = set()
        urls = set()
        timestamps = MinMax()

        for vote in queryset.iterator():
            record = {
                "user": anon.get(vote.user_id),
                "timestamp": vote.timestamp.isoformat() if vote.timestamp else None,
                "query": vote.query,
                "url": vote.url,
                "vote_type": vote.vote_type,
            }
            if writer:
                writer.write(json.dumps(record) + "\n")

            count += 1
            vote_type_counts[vote.vote_type] += 1
            queries.add(vote.query)
            urls.add(vote.url)
            timestamps.add(vote.timestamp)

        return {
            "count": count,
            "distinct_queries": len(queries),
            "distinct_urls": len(urls),
            "vote_type_counts": dict(vote_type_counts),
            "timestamp_range": timestamps.range(),
        }

    def export_domains(self, writer, anon, since):
        queryset = DomainSubmission.objects.order_by("id")
        if since:
            queryset = queryset.filter(submitted_on__date__gte=since)

        count = 0
        status_counts = Counter()
        rejection_reason_counts = Counter()
        domains = set()
        timestamps = MinMax()

        for submission in queryset.iterator():
            record = {
                "domain": submission.name,
                "user": anon.get(submission.submitted_by_id),
                "timestamp": submission.submitted_on.isoformat() if submission.submitted_on else None,
                "status": submission.status,
                "rejection_reason": submission.rejection_reason or None,
                "rejection_detail": submission.rejection_detail or None,
            }
            if writer:
                writer.write(json.dumps(record) + "\n")

            count += 1
            status_counts[submission.status] += 1
            if submission.rejection_reason:
                rejection_reason_counts[submission.rejection_reason] += 1
            domains.add(submission.name)
            timestamps.add(submission.submitted_on)

        return {
            "count": count,
            "distinct_domains": len(domains),
            "status_counts": dict(status_counts),
            "rejection_reason_counts": dict(rejection_reason_counts),
            "timestamp_range": timestamps.range(),
        }


class AnonymousUserIds:
    """Stable opaque per-export user ids, shared across all exported files."""

    def __init__(self):
        self._ids = {}
        self._contributions = Counter()

    def get(self, user_pk) -> str | None:
        if user_pk is None:
            return None
        if user_pk not in self._ids:
            self._ids[user_pk] = f"u{len(self._ids)}"
        anon_id = self._ids[user_pk]
        self._contributions[anon_id] += 1
        return anon_id

    def stats(self) -> dict:
        total = sum(self._contributions.values())
        top = self._contributions.most_common(10)
        return {
            "distinct_users": len(self._ids),
            "total_contributions": total,
            "top_10_user_share": round(sum(count for _, count in top) / total, 3) if total else None,
            "top_10_user_contributions": [count for _, count in top],
        }


class MinMax:
    def __init__(self):
        self.min = None
        self.max = None

    def add(self, value):
        if value is None:
            return
        if self.min is None or value < self.min:
            self.min = value
        if self.max is None or value > self.max:
            self.max = value

    def range(self):
        if self.min is None:
            return None
        return [self.min.isoformat(), self.max.isoformat()]
