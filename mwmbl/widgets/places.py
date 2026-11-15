"""Places for the weather widgets, from GeoNames (https://www.geonames.org, CC BY 4.0).

`cities15000.txt` (every city of more than 15,000 people) and `countryInfo.txt` come from
https://download.geonames.org/export/dump/ and are read by `index_widgets --places`; they are never committed.
Each place becomes one widget document per place-based spec, such as "York weather, United Kingdom".
"""

from collections import Counter
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path

from mwmbl.tinysearchengine.indexer import Document
from mwmbl.widgets.keys import storage_key
from mwmbl.widgets.spec import WidgetSpec
from mwmbl.widgets.tokens import widget_tokens

logger = getLogger(__name__)


@dataclass(frozen=True)
class Place:
    name: str
    tokens: tuple[str, ...]  # of the ASCII name, which is what people type
    country: str
    latitude: str  # as GeoNames writes them, so two places are the same only if they are written the same
    longitude: str
    population: int


def read_countries(path: Path) -> dict[str, str]:
    """Country names by ISO code, from countryInfo.txt."""
    countries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            columns = line.split("\t")
            countries[columns[0]] = columns[4]
    return countries


def read_places(path: Path, countries: dict[str, str], trigger_words: set[str]) -> list[Place]:
    """The places of a cities15000.txt, most populous first, each with a title no other place has.

    A name that two places in one country share gets the first-level division after it, "Springfield (MO)", and
    is dropped if that is not enough. So are places with no token a query could name them by.
    """
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        columns = line.split("\t")
        tokens = tuple(token for token in widget_tokens(columns[2]) if token not in trigger_words)
        if len(tokens) > 2 and tokens[-1] == "city":  # "New York City" is searched for as "New York"
            tokens = tokens[:-1]
        if tokens:
            population = int(columns[14])
            rows.append((population, columns[1], tokens, countries[columns[8]], columns[10], columns[4], columns[5]))
    rows.sort(key=lambda row: -row[0])
    counts = Counter((name, country) for _, name, _, country, _, _, _ in rows)
    places = []
    titles = set()
    coordinates = set()
    for population, name, tokens, country, division, latitude, longitude in rows:
        if counts[name, country] > 1 and division:
            name = f"{name} ({division})"
        if (name, country) in titles or (latitude, longitude) in coordinates:
            logger.warning("Skipping %s, %s: not distinct", name, country)
            continue
        titles.add((name, country))
        coordinates.add((latitude, longitude))
        places.append(Place(name, tokens, country, latitude, longitude, population))
    return places


def place_documents(spec: WidgetSpec, places: list[Place]) -> list[Document]:
    """The documents of one spec whose page URL has `{latitude}` and `{longitude}`, in the places' order.

    A document is filed under the spec's trigger word and the place's rarest token, which is the one that
    tells the fewest places apart from each other and so keeps pages short.
    """
    token_counts = Counter(token for place in places for token in set(place.tokens))
    documents = []
    for place in places:
        anchor = min(place.tokens, key=lambda token: (token_counts[token], token))
        tokens = [spec.trigger_word, *place.tokens]
        documents.append(
            Document(
                title=f"{place.name} {spec.trigger_word}, {place.country}",
                url=spec.page_url.format(latitude=place.latitude, longitude=place.longitude),
                extract=" ".join(tokens),
                term=storage_key(set(tokens), spec.trigger_word, anchor),
            )
        )
    return documents
