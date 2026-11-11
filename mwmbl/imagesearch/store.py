"""The store of Commons files Seed Search has found safe (mwmbl.models.CommonsImage, mwmbl/mwmbl#610).

Every candidate whose NSFW score passed is recorded, shown or not, since every safe file is to be indexed. Nothing
about the search that found it is: no query, no user, no relevance, and only the day it was found, so that a row's
times can't be matched to a request in an access log.

Recording happens on a thread of its own after the NSFW scores arrive, so it never delays a search, and a failed
write costs the store its rows and nothing else.

A revoked row holds the hashes of the content it was revoked for, and stays revoked while its file is unchanged:
only a fresh pass of changed content lifts a revocation. The exceptions are the reasons that say nothing about the
content (RECOVERABLE_REASONS), lifted by any fresh pass, and an operator's revocation, never lifted.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from logging import getLogger

from django.db import close_old_connections, transaction

from mwmbl.imagesearch import judge
from mwmbl.imagesearch.commons import commons_file_key
from mwmbl.imagesearch.prompt import CAPTION_CHARS, DESCRIPTION_CHARS, NSFW_THRESHOLD, image_text
from mwmbl.imagesearch.types import SourceImage
from mwmbl.models import CommonsImage

logger = getLogger(__name__)
Reason = CommonsImage.RevocationReason
RECOVERABLE_REASONS = {Reason.UNSETTLED, Reason.UNUSABLE, Reason.UNJUDGED}

# One thread, so that writes queue up behind each other rather than taking database connections from searches.
executor = ThreadPoolExecutor(1, thread_name_prefix="seed-images-store")


def text_hash(image: SourceImage) -> str:
    return hashlib.sha256(image_text(image).encode()).hexdigest()


def is_changed(row: CommonsImage, image: SourceImage) -> bool:
    return row.sha1 != image.sha1 or row.text_hash != text_hash(image)


def may_approve(row: CommonsImage, image: SourceImage) -> bool:
    if row.revoked_at is None or row.revocation_reason in RECOVERABLE_REASONS:
        return True
    return row.revocation_reason != Reason.OPERATOR and is_changed(row, image)


def approve(row: CommonsImage, image: SourceImage, nsfw_score: float, fingerprint: str, now: datetime) -> None:
    """Make the row this approved revision of its file, lifting any revocation. Leaves saving to the caller."""
    if row.revoked_at is not None or is_changed(row, image):
        row.approved_at = now
    row.sha1 = image.sha1
    row.text_hash = text_hash(image)
    row.title = image.title
    # Cut as the judge saw them, so that nothing is kept that it didn't read.
    row.caption = image.caption[:CAPTION_CHARS]
    row.description = image.description[:DESCRIPTION_CHARS]
    row.nsfw_score = nsfw_score
    row.nsfw_fingerprint = fingerprint
    row.license_name = image.license_name
    row.license_url = image.license_url
    row.attribution = image.attribution
    row.thumbnail_url = image.thumbnail_url
    row.thumbnail_width = image.thumbnail_width
    row.thumbnail_height = image.thumbnail_height
    row.uploaded_at = image.uploaded_at
    row.revised_at = image.revised_at
    row.last_checked_at = now
    row.revoked_at = None
    row.revocation_reason = ""


def revoke(row: CommonsImage, reason: Reason, now: datetime, image: SourceImage | None) -> None:
    """Revoke the row for the file's content now, if Commons still has it, keeping when it was first revoked.
    Leaves saving to the caller."""
    if row.revoked_at is None:
        row.revoked_at = now
    if image is not None:
        row.sha1 = image.sha1
        row.text_hash = text_hash(image)
    row.revocation_reason = reason
    row.last_checked_at = now


def record_judgments(images: list[SourceImage], nsfw_scores: list[float]) -> None:
    """Store every image whose NSFW score passes, and revoke any stored one that now fails, as just fetched from
    Commons and judged."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    fingerprint = judge.nsfw_fingerprint()
    judged = {
        commons_file_key(image.page_url): (image, score) for image, score in zip(images, nsfw_scores, strict=True)
    }
    with transaction.atomic():
        # Locked, so that a revocation made meanwhile by the recheck or an operator is never written over.
        stored = CommonsImage.objects.select_for_update().in_bulk(list(judged), field_name="page_url")
        new_rows = []
        for page_url, (image, score) in judged.items():
            row = stored.get(page_url, CommonsImage(page_url=page_url))
            passed = score < NSFW_THRESHOLD
            if passed and may_approve(row, image):
                approve(row, image, score, fingerprint, today)
            elif not passed and row.pk is not None and row.revocation_reason != Reason.OPERATOR:
                revoke(row, Reason.NSFW, today, image)
            else:
                continue
            if row.pk is None:
                new_rows.append(row)
            else:
                row.save()
        # Another process may have stored the same file since; its row is as good as this one.
        CommonsImage.objects.bulk_create(new_rows, ignore_conflicts=True)


def record_safely(images: list[SourceImage], nsfw_scores: list[float]) -> None:
    # The thread outlives requests, so it closes connections the way a request's end would.
    close_old_connections()
    try:
        record_judgments(images, nsfw_scores)
    except Exception as e:
        # Only the type: a database error's message can hold a file's name.
        logger.warning("Failed to record judged images: %s", type(e).__name__)
    finally:
        close_old_connections()


def record_later(images: list[SourceImage], nsfw_scores: list[float]) -> None:
    """Record the judgments on the store's thread, returning at once."""
    executor.submit(record_safely, images, nsfw_scores)
