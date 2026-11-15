"""Provider specs: one calculator or data service on another site, as reviewed data rather than code.

A spec gives the page a person can visit (the widget's link back), the API its data comes from, the
inputs both are filled from, and the answer's fields to show. The widget endpoint fetches only what a
spec allows: an https GET to its one API host. Specs are the JSON files in `mwmbl/widgets/providers/`.
"""

import json
import re
import string
from functools import cached_property
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PROVIDERS_DIR = Path(__file__).parent / "providers"

MAX_PATTERN_CHARS = 200
MAX_CACHE_TTL_SECONDS = 24 * 60 * 60

# `current.temperature_2m`, `daily.time[0]`
_JSON_PATH = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[\d+\])*")

InputType = Literal["number", "text", "date", "choice"]
Display = Literal["fields", "form", "forecast"]
Transform = Literal["wmo_code"]


def placeholders(template: str) -> list[str]:
    """The `{name}` placeholders of a URL template, in order. Formats and conversions are not allowed."""
    names = []
    for _, name, format_spec, conversion in string.Formatter().parse(template):
        if name is None:
            continue
        if not name.isidentifier() or format_spec or conversion:
            raise ValueError(f"Placeholders must be plain {{name}}s, got one in {template!r}")
        names.append(name)
    return names


def _check_https_url(template: str, host: str | None = None) -> None:
    parts = urlsplit(template)
    if parts.scheme != "https":
        raise ValueError(f"{template!r} must be an https URL")
    # The host is what the SSRF guard rests on: a plain lower case name, never built from inputs, with no
    # credentials or port, and the one allowed host.
    # A name, not an IP address or `localhost`: the last label starts with a letter.
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]*", parts.netloc):
        raise ValueError(f"The host of {template!r} must be a plain name")
    if host is not None and parts.netloc != host:
        raise ValueError(f"{template!r} is not on the allowed host {host!r}")


class InputSpec(BaseModel):
    """A value the URLs are filled with: from the page URL, else the query, else the default."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_]\w*$")
    type: InputType = "text"
    label: str = ""
    # A regex with one group, searched in the lowercased query; the group is the value.
    pattern: str | None = Field(default=None, max_length=MAX_PATTERN_CHARS)
    default: str | None = None

    @field_validator("pattern")
    @classmethod
    def _pattern_has_one_group(cls, pattern: str | None) -> str | None:
        if pattern is not None:
            try:
                compiled = re.compile(pattern)
            except re.error as e:
                raise ValueError(f"Bad pattern {pattern!r}: {e}") from e
            if compiled.groups != 1:
                raise ValueError(f"Pattern {pattern!r} must have exactly one group")
        return pattern


class OutputField(BaseModel):
    """One shown value: where it is in the API's JSON answer, and how to label it."""

    model_config = ConfigDict(extra="forbid")

    label: str
    path: str
    unit: str = ""
    transform: Transform | None = None  # `wmo_code`: a WMO weather code shown as words

    @field_validator("path")
    @classmethod
    def _path_is_simple(cls, path: str) -> str:
        if not _JSON_PATH.fullmatch(path):
            raise ValueError(f"{path!r} is not a JSON path such as `current.temp` or `daily.time[0]`")
        return path


class WidgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_-]+$")
    site: str  # for example `open-meteo.com`; the host of page_url
    source_name: str  # as shown to people, for example `Open-Meteo`
    page_url: str  # the provider's own page with `{input}` placeholders: the stub's `url`
    api_url: str  # with `{input}` placeholders
    api_host: str  # the only host the API is called on
    inputs: list[InputSpec] = []
    outputs: list[OutputField] = Field(min_length=1)
    display: Display = "fields"
    # False only for a documented public API whose robots.txt disallows everything, which is meant for crawlers
    # and not for calls like ours. The spec's page is still checked.
    check_api_robots: bool = True
    trigger_word: str  # puts a query in the lookup's trigger set; one of trigger_tokens
    trigger_tokens: list[str] = Field(min_length=1)  # tokens (see widget_tokens) a query must all have
    cache_ttl_seconds: int = Field(ge=1, le=MAX_CACHE_TTL_SECONDS)

    @field_validator("trigger_tokens", "trigger_word")
    @classmethod
    def _tokens_are_single_tokens(cls, value):
        for token in [value] if isinstance(value, str) else value:
            if not token or token.split() != [token] or token != token.lower():
                raise ValueError(f"{token!r} must be one lower case token")
        return value

    @model_validator(mode="after")
    def _consistent(self):
        if self.trigger_word not in self.trigger_tokens:
            raise ValueError(f"trigger_word {self.trigger_word!r} must be one of the trigger_tokens")
        _check_https_url(self.page_url, self.site)
        _check_https_url(self.api_url, self.api_host)
        names = [i.name for i in self.inputs]
        if len(set(names)) != len(names):
            raise ValueError("Input names must be unique")
        page_names = placeholders(self.page_url)
        if len(set(page_names)) != len(page_names):
            raise ValueError("A placeholder can appear only once in page_url")
        for template in (self.page_url, self.api_url):
            unknown = set(placeholders(template)) - set(names)
            if unknown:
                raise ValueError(f"Placeholders {sorted(unknown)} in {template!r} are not inputs")
        return self

    @cached_property
    def trigger_token_set(self) -> frozenset[str]:
        return frozenset(self.trigger_tokens)

    @cached_property
    def _page_url_regex(self) -> re.Pattern:
        regex = ""
        for literal, name, _, _ in string.Formatter().parse(self.page_url):
            regex += re.escape(literal)
            if name is not None:
                regex += f"(?P<{name}>[^&#/?]+)"
        return re.compile(regex)

    def match_page_url(self, url: str) -> dict[str, str] | None:
        """The still percent-encoded input values in a URL made from this spec's page template, or None."""
        match = self._page_url_regex.fullmatch(url)
        return None if match is None else match.groupdict()


def load_specs(directory: Path = PROVIDERS_DIR) -> list[WidgetSpec]:
    """Every spec in a directory's `*.json` files, in file name order. A bad file is an error, so a
    broken spec fails loudly at startup rather than quietly dropping a widget."""
    if not directory.is_dir():
        return []
    specs = []
    for path in sorted(directory.glob("*.json")):
        try:
            specs.append(WidgetSpec.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        except (ValueError, ValidationError) as e:
            raise ValueError(f"Bad widget spec {path.name}: {e}") from e
    names = [spec.name for spec in specs]
    if len(set(names)) != len(names):
        raise ValueError(f"Duplicate widget spec names in {directory}: {sorted(names)}")
    return specs
