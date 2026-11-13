"""`GET /api/v2/widgets/`: the values of one widget, fetched from the provider's API for a widget stub's `url`.

The provider is only ever called with an https GET to the spec's API host (mwmbl.widgets.fetch), with the
inputs cleaned by type and percent-encoded, and only if its robots.txt allows Mwmbl.
"""

import hashlib
import json
import re
import time
from collections.abc import Sequence
from datetime import date
from urllib.parse import quote, unquote, urlsplit

from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection
from ninja import Query, Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from mwmbl.crawler.retrieve import RobotsUnavailable, robots_allowed
from mwmbl.search_callers import client_network
from mwmbl.widgets.fetch import FetchError, fetch_json
from mwmbl.widgets.lookup import WidgetStub
from mwmbl.widgets.spec import Display, InputSpec, InputType, OutputField, WidgetSpec

MAX_TEXT_CHARS = 100
MAX_VALUE_CHARS = 200
NUMBER = re.compile(r"-?\d{1,15}(?:\.\d{1,15})?")
CHOICE = re.compile(r"[A-Za-z0-9_-]{1,32}")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
PATH_PART = re.compile(r"[A-Za-z_]\w*|\[(\d+)\]")
LOCK_POLL_SECONDS = 0.1


class WidgetField(Schema):
    label: str
    value: str = Field(description="Plain text. A client must escape it, never treat it as markup.")
    unit: str


class WidgetInput(Schema):
    name: str
    type: InputType
    label: str
    value: str


class WidgetResponse(WidgetStub):
    display: Display
    fields: list[WidgetField]
    inputs: list[WidgetInput] = Field(
        description="The inputs the widget was filled with, for a client that lets people change them."
    )
    share_url: str = Field(description='The provider\'s page: link to it with `rel="noopener nofollow"`.')


def clean_input(input_spec: InputSpec, value: str) -> str:
    """The value if it is a valid one for the input's type, else a 422."""
    value = value.strip()
    if input_spec.type == "number":
        valid = NUMBER.fullmatch(value) is not None
    elif input_spec.type == "date":
        try:
            valid = date.fromisoformat(value).isoformat() == value
        except ValueError:
            valid = False
    elif input_spec.type == "choice":
        valid = CHOICE.fullmatch(value) is not None
    else:
        valid = 0 < len(value) <= MAX_TEXT_CHARS and CONTROL_CHARACTERS.search(value) is None
    if not valid:
        raise HttpError(422, f"Invalid value for {input_spec.name}")
    return value


def input_values(spec: WidgetSpec, url: str, q: str) -> dict[str, str]:
    """Each input from the page URL, else the query, else the default."""
    from_url = spec.match_page_url(url)
    assert from_url is not None
    lowered_query = q.lower()
    values = {}
    for input_spec in spec.inputs:
        raw = unquote(from_url[input_spec.name]) if input_spec.name in from_url else None
        if raw is None and input_spec.pattern is not None:
            match = re.search(input_spec.pattern, lowered_query)
            raw = match.group(1) if match else None
        if raw is None:
            raw = input_spec.default
        if raw is None:
            raise HttpError(422, f"No value for {input_spec.name}")
        values[input_spec.name] = clean_input(input_spec, raw)
    return values


def output_fields(spec: WidgetSpec, data) -> list[WidgetField]:
    """The spec's outputs found in the answer, as plain text. Anything but a string, number or boolean is dropped."""
    fields = []
    for output in spec.outputs:
        value = find_value(data, output)
        if isinstance(value, bool):
            value = str(value).lower()
        if isinstance(value, (str, int, float)):
            text = CONTROL_CHARACTERS.sub(" ", str(value))[:MAX_VALUE_CHARS]
            fields.append(WidgetField(label=output.label, value=text, unit=output.unit))
    return fields


def find_value(data, output: OutputField):
    for match in PATH_PART.finditer(output.path):
        index = match.group(1)
        if index is not None and isinstance(data, list) and int(index) < len(data):
            data = data[int(index)]
        elif index is None and isinstance(data, dict) and match.group(0) in data:
            data = data[match.group(0)]
        else:
            return None
    return data


def within_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    window_key = f"widgets:rate:{key}:{int(time.time()) // window_seconds}"
    cache.add(window_key, 0, timeout=window_seconds)
    return cache.incr(window_key) <= limit


def cached_fields(spec: WidgetSpec, api_url: str) -> list[WidgetField]:
    """The fields for an API URL: cached for the spec's TTL, and one request at a time fetches on a miss."""
    redis = get_redis_connection("default")
    digest = hashlib.sha256(api_url.encode()).hexdigest()
    key = f"widgets:answer:{spec.name}:{digest}"
    cached = redis.get(key)
    if cached is None and redis.set(f"{key}:lock", 1, nx=True, ex=settings.WIDGET_LOCK_SECONDS):
        try:
            if not within_rate_limit(f"spec:{spec.name}", settings.WIDGET_SPEC_FETCHES_PER_SECOND, 1):
                raise HttpError(429, "Too many requests")
            fields = output_fields(spec, fetch_json(api_url, spec.api_host))
            redis.setex(key, spec.cache_ttl_seconds, json.dumps([field.dict() for field in fields]))
            return fields
        except FetchError as e:
            raise HttpError(502, "The provider could not be reached") from e
        finally:
            redis.delete(f"{key}:lock")
    # Someone else holds the miss: wait for their answer rather than asking the provider again. If they
    # fail the lock goes and the answer never comes, so we fail too instead of waiting out the lock.
    while cached is None and redis.exists(f"{key}:lock"):
        time.sleep(LOCK_POLL_SECONDS)
        cached = redis.get(key)
    cached = cached or redis.get(key)
    if cached is None:
        raise HttpError(502, "The provider could not be reached")
    return [WidgetField(**field) for field in json.loads(cached)]


def widgets_router(specs: Sequence[WidgetSpec]) -> Router:
    router = Router(tags=["Widgets"])

    @router.get(
        "",
        response=WidgetResponse,
        auth=None,
        summary="Widget values",
        description="The values of the widget a search response's stub names, fetched from the provider (and cached). "
        "Pass the stub's `url` as `url` and the search as `q`. 404 if no widget has that URL or the provider's "
        "robots.txt does not allow Mwmbl.",
    )
    def widget(request, url: str = Query(max_length=500), q: str = Query("", max_length=200)):
        if not within_rate_limit(f"ip:{client_network(request)}", settings.WIDGET_RATE_LIMIT_PER_MINUTE, 60):
            raise HttpError(429, "Too many requests")
        spec = next((spec for spec in specs if spec.match_page_url(url) is not None), None)
        if spec is None:
            raise HttpError(404, "No such widget")
        values = input_values(spec, url, q)
        api_url = spec.api_url.format(**{name: quote(value, safe="") for name, value in values.items()})
        assert urlsplit(api_url).netloc == spec.api_host
        try:
            allowed = robots_allowed(api_url, get_redis_connection("default"))
        except RobotsUnavailable:
            allowed = False
        if not allowed:
            raise HttpError(404, "No such widget")
        fields = cached_fields(spec, api_url)
        inputs = [WidgetInput(name=i.name, type=i.type, label=i.label, value=values[i.name]) for i in spec.inputs]
        return WidgetResponse(
            title=spec.source_name,
            url=url,
            source_name=spec.source_name,
            display=spec.display,
            fields=fields,
            inputs=inputs,
            share_url=url,
        )

    return router
