"""Write the widget documents of the provider specs into the search index.

    uv run manage.py index_widgets --settings=mwmbl.settings_dev

One document per spec, filed under its widget key (see mwmbl.widgets.keys). A site whose robots.txt
does not allow Mwmbl its page or API is skipped, and its document is removed, as is the document of a spec
that is gone or changed since the last run. The keys written are remembered in DATA_PATH, because the
index cannot be searched for documents a spec used to produce.
"""

import json
from collections import defaultdict
from collections.abc import Callable
from logging import getLogger
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django_redis import get_redis_connection

from mwmbl.crawler.retrieve import RobotsUnavailable, robots_allowed
from mwmbl.indexer.index_batches import index_widget_documents
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, TinyIndex
from mwmbl.widgets.keys import storage_key
from mwmbl.widgets.spec import WidgetSpec, load_specs

logger = getLogger(__name__)

KEYS_FILE_NAME = "widget-keys.json"


def widget_document(spec: WidgetSpec) -> Document:
    """The one document of a spec, stored under its widget key (as its term).

    Its extract is its trigger tokens, which the lookup reads back; its URL is the spec's page, and its title names
    the widget, so both are the same on every run and differ from every other spec's.
    """
    anchor = next((token for token in spec.trigger_tokens if token != spec.trigger_word), spec.trigger_word)
    key = storage_key(set(spec.trigger_tokens), spec.trigger_word, anchor)
    return Document(
        title=f"{spec.name.replace('-', ' ').capitalize()}, {spec.site}",
        url=spec.page_url,
        extract=" ".join(spec.trigger_tokens),
        term=key,
        source=DocumentSource.WIDGET,
    )


def site_allows_mwmbl(spec: WidgetSpec, redis) -> bool:
    """Whether robots.txt lets Mwmbl fetch the spec's page and its API. An unreadable robots.txt does not."""
    api_url = spec.api_url.format(**{input_spec.name: "0" for input_spec in spec.inputs})
    try:
        return robots_allowed(spec.page_url, redis) and robots_allowed(api_url, redis)
    except RobotsUnavailable:
        return False


def remove_stale_widgets(index: TinyIndex, keys: set[str], wanted: set[tuple[str, str]]):
    """Delete the widget documents under these keys whose (key, URL) is not wanted.

    Only widget documents are touched, never a crawled document that shares the page. It does not go through
    purge_blacklisted.purge_documents, which removes by URL on the pages a document's text hashes to, and a
    widget is not stored there, while a crawled page might have the same URL.
    """
    keys_by_page = defaultdict(set)
    for key in keys:
        keys_by_page[index.get_key_page_index(key)].add(key)
    for page_index, page_keys in keys_by_page.items():
        with index.page(page_index) as page:
            kept = [
                document
                for document in page.documents
                if not (
                    document.source == DocumentSource.WIDGET
                    and document.term in page_keys
                    and (document.term, document.url) not in wanted
                )
            ]
            if len(kept) < len(page.documents):
                page.store(kept)


def sync_widgets(
    index_path: str, specs: list[WidgetSpec], allowed: Callable[[WidgetSpec], bool], previous_keys: set[str]
) -> set[str]:
    """Make the index's widget documents those of the allowed specs, and return their keys.

    The documents of a blocked site, and those a changed or removed spec used to produce (their keys are
    `previous_keys`), are deleted first; then the rest are written, replacing any older copy by URL.
    """
    documents = [widget_document(spec) for spec in specs]
    titles = [document.title for document in documents]
    if len(set(titles)) != len(titles):
        raise ValueError(f"Widget titles must be unique, got {sorted(titles)}")
    wanted = [document for spec, document in zip(specs, documents) if allowed(spec)]
    managed_keys = previous_keys | {document.term for document in documents}
    with TinyIndex(Document, index_path, "w") as index:
        remove_stale_widgets(index, managed_keys, {(document.term, document.url) for document in wanted})
    index_widget_documents([(document, document.term) for document in wanted], index_path)
    return {document.term for document in wanted}


def load_keys(path: Path) -> set[str]:
    return set(json.loads(path.read_text())) if path.exists() else set()


def save_keys(path: Path, keys: set[str]):
    path.write_text(json.dumps(sorted(keys)))


class Command(BaseCommand):
    help = "Index the widget documents of the provider specs"

    def handle(self, *args, **options):
        redis = get_redis_connection("default")
        specs = load_specs()
        keys_path = Path(settings.DATA_PATH) / KEYS_FILE_NAME
        previous_keys = load_keys(keys_path)

        def allowed(spec: WidgetSpec) -> bool:
            site_allows = site_allows_mwmbl(spec, redis)
            if not site_allows:
                logger.warning("Not indexing %s: %s does not allow Mwmbl", spec.name, spec.site)
            return site_allows

        keys = sync_widgets(str(Path(settings.DATA_PATH) / settings.INDEX_NAME), specs, allowed, previous_keys)
        save_keys(keys_path, keys)
        self.stdout.write(f"Indexed {len(keys)} of {len(specs)} widgets")
