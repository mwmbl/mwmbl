"""Crawl and score domains that have no evidence yet.

Two populations, both needed:

    # give the moderation queue suggestions on day one
    uv run manage.py backfill_domain_evidence --status PENDING

    # give the model page text to train on
    uv run manage.py backfill_domain_evidence --status APPROVED --status REJECTED --since 2025-01-01

Resumable: it only ever looks at domains with no DomainEvidence row, so interrupting it and
running it again picks up where it stopped.

``--since`` matters for the training population. The crawl happens now but the labels were
made up to two years ago, so the further back you reach the more the page text describes a
site that has changed since a moderator judged it.
"""
import time

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date

from django.db import transaction

from mwmbl.crawler.env_vars import CRAWL_DELAY_SECONDS
from mwmbl.indexer.blacklist_snapshot import get_redis
from mwmbl.models import DomainEvidence, DomainSubmission
from mwmbl.moderation.evidence import crawl_domain, store_evidence, store_failure
from mwmbl.moderation.suggest import refresh_suggestion
from mwmbl.moderation.training_data import is_trainable_domain


class Command(BaseCommand):
    help = "Crawl submitted domains that have no moderation evidence yet"

    def add_arguments(self, parser):
        parser.add_argument("--status", action="append", default=None,
                            choices=list(DomainSubmission.DOMAIN_SUBMISSION_STATUS))
        parser.add_argument("--since", type=parse_date, default=None)
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--delay", type=float, default=CRAWL_DELAY_SECONDS,
                            help="Seconds to wait between domains")

    def handle(self, *args, **options):
        domains = self._domains_to_crawl(options)
        self.stdout.write(f"{len(domains)} domains to crawl")

        redis = get_redis()
        for number, domain in enumerate(domains, start=1):
            try:
                # Both halves in one transaction, so an interrupted run never leaves a READY
                # row with no suggestion on it - which the freshness check would then skip.
                with transaction.atomic():
                    evidence = store_evidence(domain, crawl_domain(domain, redis))
                    refresh_suggestion(evidence)
                self.stdout.write(
                    f"[{number}/{len(domains)}] {domain}: {evidence.suggested_action or '?'} "
                    f"({evidence.state})")
            except Exception as exception:
                # One unfetchable domain must not end a backfill of thousands.
                store_failure(domain, type(exception).__name__)
                self.stderr.write(f"[{number}/{len(domains)}] {domain}: FAILED ({exception})")
            if options["delay"]:
                time.sleep(options["delay"])

    @staticmethod
    def _domains_to_crawl(options) -> list[str]:
        submissions = DomainSubmission.objects.all()
        if options["status"]:
            submissions = submissions.filter(status__in=options["status"])
        if options["since"]:
            submissions = submissions.filter(submitted_on__date__gte=options["since"])

        already_have = set(DomainEvidence.objects.values_list("domain", flat=True))
        seen, domains = set(), []
        for name in submissions.order_by("submitted_on").values_list("name", flat=True):
            if name in seen or name in already_have or not is_trainable_domain(name):
                continue
            seen.add(name)
            domains.append(name)
            if options["limit"] and len(domains) >= options["limit"]:
                break
        return domains
