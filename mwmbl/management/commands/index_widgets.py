"""Write the widget documents of the provider specs into the search index (`manage.py index_widgets`).

One document per spec, filed under its widget key (see mwmbl.widgets.keys). A site whose robots.txt
does not allow Mwmbl its page or API is skipped, and its document is removed, as is the document of a spec
that is gone or changed since the last run. The keys written are remembered in DATA_PATH, because the
index cannot be searched for documents a spec used to produce.

A spec whose page URL has placeholders (the weather ones) is indexed once per place, from
`--places cities15000.txt countryInfo.txt` (GeoNames, see mwmbl.widgets.places). A plain run leaves the place
documents alone, and their keys are remembered apart from the others'; `--places` syncs them again and
`--no-places` removes them all.
"""

import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from logging import getLogger
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django_redis import get_redis_connection

from mwmbl.crawler.retrieve import RobotsUnavailable, robots_allowed
from mwmbl.indexer.index_batches import index_widget_documents
from mwmbl.tinysearchengine.indexer import Document, DocumentSource, TinyIndex
from mwmbl.widgets.keys import storage_key
from mwmbl.widgets.places import Place, place_documents, read_countries, read_places
from mwmbl.widgets.spec import WidgetSpec, load_specs, placeholders

logger = getLogger(__name__)

KEYS_FILE_NAME = "widget-keys.json"
PLACE_KEYS_FILE_NAME = "widget-place-keys.json"


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
    zeros = {input_spec.name: "0" for input_spec in spec.inputs}
    try:
        return robots_allowed(spec.page_url.format(**zeros), redis) and (
            not spec.check_api_robots or robots_allowed(spec.api_url.format(**zeros), redis)
        )
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
    index_path: str,
    specs: list[WidgetSpec],
    allowed: Callable[[WidgetSpec], bool],
    previous_keys: set[str],
    places: Sequence[Place] = (),
) -> set[str]:
    """Make the index's widget documents those of the allowed specs, and return their keys.

    The documents of a blocked site, and those a changed or removed spec used to produce (their keys are
    `previous_keys`), are deleted first; then the rest are written. It adds the keys it may write to `previous_keys`.
    """
    documents_by_spec = {}
    for spec in specs:
        has_places = bool(placeholders(spec.page_url))
        documents_by_spec[spec.name] = place_documents(spec, places) if has_places else [widget_document(spec)]
    documents = [document for spec_documents in documents_by_spec.values() for document in spec_documents]
    for what, values in (("titles", [d.title for d in documents]), ("URLs", [d.url for d in documents])):
        if len(set(values)) != len(values):
            raise ValueError(f"Widget {what} must be unique")
    allowed_specs = [spec for spec in specs if allowed(spec)]
    wanted = [document for spec in allowed_specs for document in documents_by_spec[spec.name]]
    previous_keys |= {document.term for document in documents}
    with TinyIndex(Document, index_path, "w") as index:
        remove_stale_widgets(index, previous_keys, {(document.term, document.url) for document in wanted})
    index_widget_documents([(document, document.term) for document in wanted], index_path)
    return {document.term for document in wanted}


def load_keys(path: Path) -> set[str]:
    return set(json.loads(path.read_text())) if path.exists() else set()


def save_keys(path: Path, keys: set[str]):
    path.write_text(json.dumps(sorted(keys)))


def sync_and_save(
    keys_path: Path, index_path: str, specs: list[WidgetSpec], allowed: Callable[[WidgetSpec], bool], places=()
) -> set[str]:
    previous_keys = load_keys(keys_path)
    try:
        keys = sync_widgets(index_path, specs, allowed, previous_keys, places)
    except BaseException:
        save_keys(keys_path, previous_keys)  # what may have been written, so the next run can remove it
        raise
    save_keys(keys_path, keys)
    return keys


class Command(BaseCommand):
    help = "Index the widget documents of the provider specs"

    def add_arguments(self, parser):
        places_group = parser.add_mutually_exclusive_group()
        places_group.add_argument(
            "--places",
            nargs=2,
            metavar=("CITIES_TSV", "COUNTRY_INFO_TXT"),
            type=Path,
            help="Sync the place documents of the weather widgets from GeoNames cities15000.txt and countryInfo.txt",
        )
        places_group.add_argument(
            "--no-places", action="store_true", help="Remove all place documents (a plain run leaves them alone)"
        )

    def handle(self, *args, **options):
        redis = get_redis_connection("default")
        specs = load_specs()
        place_specs = [spec for spec in specs if placeholders(spec.page_url)]
        other_specs = [spec for spec in specs if not placeholders(spec.page_url)]
        data_path = Path(settings.DATA_PATH)
        index_path = str(data_path / settings.INDEX_NAME)

        def allowed(spec: WidgetSpec) -> bool:
            site_allows = site_allows_mwmbl(spec, redis)
            if not site_allows:
                logger.warning("Not indexing %s: %s does not allow Mwmbl", spec.name, spec.site)
            return site_allows

        keys = sync_and_save(data_path / KEYS_FILE_NAME, index_path, other_specs, allowed)
        message = f"Indexed {len(keys)} of {len(other_specs)} widgets"
        if options["places"]:
            cities_path, countries_path = options["places"]
            trigger_words = {spec.trigger_word for spec in specs}
            places = read_places(cities_path, read_countries(countries_path), trigger_words)
            place_keys = sync_and_save(data_path / PLACE_KEYS_FILE_NAME, index_path, place_specs, allowed, places)
            message += f" and {len(place_keys)} place widgets"
        elif options["no_places"]:
            sync_and_save(data_path / PLACE_KEYS_FILE_NAME, index_path, place_specs, lambda spec: False)
            message += ", and removed the place widgets"
        self.stdout.write(message)
