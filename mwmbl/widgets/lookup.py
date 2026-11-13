"""Finding the widgets a query triggers, from the index and the specs alone: no outbound call, no database."""

from collections.abc import Sequence
from functools import lru_cache
from typing import Literal

from ninja import Schema
from pydantic import Field

from mwmbl.tinysearchengine.indexer import Document, DocumentSource, TinyIndex
from mwmbl.widgets.keys import trigger_query_keys
from mwmbl.widgets.spec import WidgetSpec, load_specs
from mwmbl.widgets.tokens import widget_tokens


class WidgetStub(Schema):
    """A widget that applies to the query. Its values are not in the search response: a client that wants them
    asks the widget endpoint for `url`."""

    type: Literal["widget"] = "widget"
    title: str = Field(description="Names this instance, for example `York weather, United Kingdom`.")
    url: str = Field(
        description='The provider\'s page with the inputs filled in: the link back (use `rel="noopener nofollow"`) '
        "and what the widget endpoint is asked about."
    )
    source_name: str = Field(description="The provider as shown to people, for example `Open-Meteo`.")


class WidgetFinder:
    def __init__(self, specs: Sequence[WidgetSpec]):
        self.specs = list(specs)
        self.trigger_words = frozenset(spec.trigger_word for spec in self.specs)

    def find(self, q: str, tiny_index: TinyIndex) -> list[WidgetStub]:
        """The widgets for a query, at most one per spec, the first stored when a spec matches several."""
        if not self.specs:
            return []
        tokens = widget_tokens(q)
        # By far the commonest case, and all it costs is the tokenizer.
        if self.trigger_words.isdisjoint(tokens):
            return []
        query_tokens = set(tokens)
        # Per spec, the match with the most trigger tokens: "weather in new york" is New York's, not York's.
        # Ties go to the one stored first.
        best: dict[int, tuple[int, WidgetStub]] = {}
        for key in trigger_query_keys(tokens, self.trigger_words):
            for document in tiny_index.retrieve(key):
                # Crawled text can be stored under a widget key too.
                if document.source != DocumentSource.WIDGET:
                    continue
                match = self._match(document, query_tokens)
                if match is not None and match[1] > best.get(match[0], (-1,))[0]:
                    best[match[0]] = (
                        match[1],
                        WidgetStub(
                            title=document.title, url=document.url, source_name=self.specs[match[0]].source_name
                        ),
                    )
        return [stub for _, (_, stub) in sorted(best.items())]

    def _match(self, document: Document, query_tokens: set[str]) -> tuple[int, int] | None:
        """The spec a document is an instance of and how many tokens it triggers on, if the query has them all."""
        tokens = set(document.extract.split())  # a widget document's extract is its trigger tokens
        for i, spec in enumerate(self.specs):
            # The URL decides the spec, so a document cannot point a widget at a host its spec does not allow.
            if spec.match_page_url(document.url) is None:
                continue
            all_tokens = spec.trigger_token_set | tokens
            if all_tokens <= query_tokens:
                return i, len(all_tokens)
        return None


@lru_cache(maxsize=1)
def default_finder() -> WidgetFinder:
    """The finder for the specs in the providers directory, which are read once."""
    return WidgetFinder(load_specs())
