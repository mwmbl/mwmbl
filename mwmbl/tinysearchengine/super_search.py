"""Super Search — multi-source streaming search endpoint.

Fans out a query to several external search APIs, re-ranks the union with the
existing LTR model, and for any result above the threshold fetches the page,
picks promising outbound links, and re-ranks those too. Progress and results
are streamed to the client over Server-Sent Events.

The SSE event payloads are defined by the ``Schema`` classes in the "Event
payload schemas" section below; those classes are both serialized onto the wire
and used to generate the OpenAPI documentation, so the two cannot drift.

See plan: /api/v2/super-search/
"""
import asyncio
import copy
import heapq
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import re
import time
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import httpx
import redis
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import StreamingHttpResponse
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import BaseModel, Field

from mwmbl.crawler.retrieve import crawl_url
from mwmbl.indexer.index_batches import index_results_against_query
from mwmbl.quota import (
    check_rate_limit,
    decrement_monthly_super_search,
    increment_monthly_super_search,
)
from mwmbl.search_auth import authenticate_user
from mwmbl.search_setup import index_path, ltr_model
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.ltr_rank import score_documents
from mwmbl.tinysearchengine.mmr_rank import mmr_rerank
from mwmbl.tinysearchengine.rank import score_result_whole
from mwmbl.tinysearchengine.super_search_sources import SOURCES
from mwmbl.tinysearchengine.super_search_select import bandit as ss_bandit
from mwmbl.tinysearchengine.super_search_select import profiles as ss_profiles
from mwmbl.tinysearchengine.super_search_select.policy import select_sources
from mwmbl.tinysearchengine.super_search_select.rewards import (
    SelectionContext,
    compute_rewards,
    log_impression,
)
from mwmbl.tokenizer import tokenize

logger = logging.getLogger(__name__)

router = Router(tags=["Super Search"])

KEEPALIVE_INTERVAL = 5.0  # seconds between idle keepalive comments
HTTP_USER_AGENT = "mwmbl-super-search/0.1 (+https://mwmbl.org)"
_SENTINEL: object = object()
_URL_EXT_RE = re.compile(r"\.\w{1,5}$")
_URL_TOKEN_RE = re.compile(r"[-_+]+")

# Dedicated, bounded pool for the (synchronous, self-timeout-bounded) crawl_url
# calls. Keeping crawls off the default executor stops a burst of page fetches
# from starving the threads used by score_documents / the ORM, and caps how many
# crawl threads can keep running after the pipeline deadline fires.
_CRAWL_EXECUTOR = ThreadPoolExecutor(
    max_workers=getattr(settings, "SUPER_SEARCH_CRAWL_WORKERS", 8),
    thread_name_prefix="ss-crawl",
)

# Redis connection for caching robots.txt
_redis = None


def _get_redis() -> redis.Redis:
    """Get or create Redis connection for robots.txt caching."""
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def _crawl(url: str):
    """Run the synchronous crawl_url on the dedicated crawl executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_CRAWL_EXECUTOR, crawl_url, url, _get_redis())


async def _update_profile(name: str, docs: list[Document]) -> None:
    """Fold a source's results into its content profile (off-thread, best-effort)."""
    try:
        await asyncio.to_thread(ss_profiles.update_profile, name, docs)
    except Exception:
        logger.exception("super-search profile update failed for %s", name)


# ---------------------------------------------------------------------------
# Event payload schemas
#
# These ``Schema`` classes are the single source of truth for the SSE event
# payloads: every ``emit(...)`` call constructs one of them and the serializer
# (`_sse_frame`) dumps it to JSON, while the OpenAPI spec is generated from the
# same classes (see ``_event_oneof``). The wire format therefore cannot drift
# from the documentation, and pydantic raises if a payload is ever built with
# the wrong shape.
# ---------------------------------------------------------------------------

class ResultItem(Schema):
    """A single ranked search result.

    Carried directly by ``result_promoted`` events and inside the ``results``
    list. ``title`` and ``extract`` are nullable because upstream sources may
    omit them.
    """
    url: str = Field(description="Canonical URL of the result.",
                     examples=["https://docs.rs/tokio"])
    title: str | None = Field(default=None, description="Page title, if known.",
                              examples=["Tokio — asynchronous Rust runtime"])
    extract: str | None = Field(default=None, description="Short text snippet, if known.",
                                examples=["Tokio is an asynchronous runtime for Rust…"])
    score: float = Field(description="Relevance score (higher is better), rounded to 4 dp.",
                         examples=[1.8423])
    source: str = Field(
        description=(
            "Originating source for `result_promoted` (one of the Super Search "
            "sources, e.g. `github`); empty string for items in the final "
            "`results` ranking, which merges all sources."
        ),
        examples=["github", ""],
    )
    origin: Literal["direct", "final"] = Field(
        description=(
            "`direct` for a result promoted straight from a source; `final` for "
            "an item in the authoritative merged ranking."
        ),
        examples=["direct"],
    )


class SourceStartedEvent(Schema):
    """`source_started` — a source's query task has been launched (one per source)."""
    source: str = Field(description="Name of the source whose query just started.",
                        examples=["hn"])


class SourceReturnedEvent(Schema):
    """`source_returned` — a source finished successfully."""
    source: str = Field(description="Name of the source that returned.", examples=["github"])
    count: int = Field(description="Number of raw documents the source returned.", examples=[10])


class SourceFailedEvent(Schema):
    """`source_failed` — a source errored or timed out and contributed nothing."""
    source: str = Field(description="Name of the source that failed.", examples=["arxiv"])
    error: str = Field(
        description="Failure reason: `\"timeout\"` or an exception message.",
        examples=["timeout"],
    )


class ResultPromotedEvent(ResultItem):
    """`result_promoted` — a returned doc entered the live top-K (and will have
    its outbound links followed). Same shape as :class:`ResultItem` with
    ``origin="direct"``.
    """


class PageFetchedEvent(Schema):
    """`page_fetched` — a promoted page was crawled."""
    url: str = Field(description="URL of the crawled page.",
                     examples=["https://docs.rs/tokio"])
    links: int = Field(description="Number of outbound links discovered on the page.",
                       examples=[42])


class LinkFollowedEvent(Schema):
    """`link_followed` — an outbound link from a crawled page was fetched and added
    to the candidate pool.
    """
    url: str = Field(description="URL of the followed link.",
                     examples=["https://tokio.rs/tokio/tutorial"])
    from_: str = Field(alias="from", description="URL of the parent page the link came from.",
                       examples=["https://docs.rs/tokio"])


class ResultsEvent(Schema):
    """`results` — the current authoritative ranking.

    Emitted progressively after each source returns, and once more as the final
    ranking. Clients should **replace** their displayed list on each event.
    """
    results: list[ResultItem] = Field(description="Ranked results, best first.")
    count: int = Field(description="Number of results in this ranking.", examples=[100])


class ErrorEvent(Schema):
    """`error` — the pipeline crashed; the stream will end."""
    message: str = Field(description="Error message.", examples=["internal error"])


class DoneEvent(Schema):
    """`done` — terminal event sent once when the stream finishes."""
    reason: Literal["complete", "timed_out", "cancelled", "error"] = Field(
        description="Why the stream ended.", examples=["complete"],
    )
    elapsed_seconds: float = Field(description="Total wall-clock time for the request.",
                                   examples=[8.123])
    monthly_usage: int = Field(description="Caller's Super Search requests used this month "
                                           "(including this one).", examples=[3])
    monthly_limit: int = Field(description="Caller's monthly Super Search quota.", examples=[100])
    pages_indexed: int = Field(
        default=0,
        description="Number of distinct new pages (URLs) added to the Mwmbl index as a "
                    "result of this search. Can exceed the number of results returned, "
                    "since a page may match a unigram or bigram of the query without "
                    "matching the whole query.",
        examples=[7],
    )


# Maps each SSE event name to the model describing its `data` payload. Used both
# to document the response in OpenAPI and as a reference for the emit sites.
_EVENT_MODELS: dict[str, type[Schema]] = {
    "source_started": SourceStartedEvent,
    "source_returned": SourceReturnedEvent,
    "source_failed": SourceFailedEvent,
    "result_promoted": ResultPromotedEvent,
    "page_fetched": PageFetchedEvent,
    "link_followed": LinkFollowedEvent,
    "results": ResultsEvent,
    "error": ErrorEvent,
    "done": DoneEvent,
}


def _inline_defs(schema: dict) -> dict:
    """Inline pydantic ``$defs`` so the schema is self-contained.

    ``model_json_schema`` emits nested models (e.g. ``ResultItem``) as
    ``$ref``s into a top-level ``$defs``. Those refs do not resolve once the
    schema is embedded under ``openapi_extra``, so we splice the definitions in.
    """
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                return resolve(copy.deepcopy(defs[ref.rsplit("/", 1)[-1]]))
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


def _event_oneof() -> list[dict]:
    """Build a ``oneOf`` of the event `data` payloads for the OpenAPI response."""
    branches = []
    for name, model in _EVENT_MODELS.items():
        s = _inline_defs(model.model_json_schema(by_alias=True))
        s["title"] = name  # so the spec/Swagger labels each branch by event name
        branches.append(s)
    return branches


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse_frame(event_type: str, data: Any) -> bytes:
    if isinstance(data, BaseModel):
        data = data.model_dump(by_alias=True)
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


def _result_payload(doc: Document, score: float, source: str, origin: Literal["direct", "final"]) -> ResultItem:
    return ResultItem(
        url=doc.url,
        title=doc.title,
        extract=doc.extract,
        score=round(score, 4),
        source=source,
        origin=origin,
    )


def _doc_passes_term_filter(doc: Document, terms: list[str]) -> bool:
    """Return True if more than half the query terms have a whole-word match in the document.

    Uses \b word boundaries so that a query term like "exa" does not match
    "example" via substring. The combined title+extract+URL is checked so that
    a term present in any of those fields counts.
    """
    if not terms:
        return True
    text = f"{doc.title or ''} {doc.extract or ''} {doc.url or ''}".lower()
    matches = sum(
        1 for t in terms
        if re.search(rf'\b{re.escape(t)}\b', text)
    )
    return matches > len(terms) / 2


def _heuristic_score_docs(query: str, docs: list[Document]) -> list[float]:
    terms = tokenize(query)
    if not terms:
        return [0.0] * len(docs)
    return [score_result_whole(terms, doc, is_complete=True) for doc in docs]


def _url_term_score(url: str, terms: list[str]) -> int:
    """Count how many query terms appear as whole words anywhere in the URL.

    Uses \b word boundaries so that "exa" does not score a URL containing
    "example". URL separators (., -, /) count as word boundaries, so
    "kagi.com/discord" correctly matches both "kagi" and "discord".
    """
    url_lower = url.lower()
    return sum(1 for t in terms if re.search(rf'\b{re.escape(t)}\b', url_lower))


def _title_from_url(url: str) -> str:
    """Cheap human-readable proxy used to score outbound links with the LTR model."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        return parsed.netloc
    last = unquote(segments[-1])
    last = _URL_EXT_RE.sub("", last)
    return _URL_TOKEN_RE.sub(" ", last).strip() or parsed.netloc



# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def _call_source(name: str, fn, client: httpx.AsyncClient, query: str, limit: int):
    try:
        docs = await asyncio.wait_for(
            fn(client, query, limit),
            timeout=settings.SUPER_SEARCH_PER_SOURCE_TIMEOUT,
        )
        return name, docs, None
    except asyncio.TimeoutError:
        return name, [], "timeout"
    except Exception as e:  # noqa: BLE001 — adapters shouldn't raise but be defensive
        logger.info("super-search source %s raised: %s", name, e)
        return name, [], str(e)


async def _follow_links(
    parent: Document, query: str, emit, all_docs: list[Document],
    last_results_key: list, lock: asyncio.Lock,
) -> None:
    """Crawl the parent URL, score its outbound links, and collect the best for final ranking."""
    max_links = settings.SUPER_SEARCH_MAX_LINKS_PER_PAGE

    try:
        result = await _crawl(parent.url)
    except Exception as e:  # noqa: BLE001
        logger.info("crawl_url failed for %s: %s", parent.url, e)
        return

    content = result.get("content") if isinstance(result, dict) else None
    if not content:
        return

    raw_links = list(content.get("links") or []) + list(content.get("extra_links") or [])
    raw_links = raw_links[:50]
    await emit("page_fetched", PageFetchedEvent(url=parent.url, links=len(raw_links)))

    parent_title = content.get("title") or parent.title
    parent_extract = content.get("extract") or parent.extract or ""
    if parent_title:
        all_docs.append(Document(title=parent_title, url=parent.url, extract=parent_extract))

    if not raw_links:
        await _emit_final_results(query, all_docs, emit, last_results_key, lock)
        return

    terms = tokenize(query)
    proxy_docs = [Document(title=_title_from_url(u), url=u, extract="") for u in raw_links]
    proxy_scores = [_url_term_score(d.url, terms) for d in proxy_docs]

    ranked = sorted(zip(proxy_docs, proxy_scores), key=lambda x: -x[1])[:max_links]
    if not ranked:
        return

    fetches = await asyncio.gather(
        *[_crawl(d.url) for d, _ in ranked],
        return_exceptions=True,
    )

    for (proxy_doc, _), fetched in zip(ranked, fetches):
        if isinstance(fetched, Exception):
            continue
        c = fetched.get("content") if isinstance(fetched, dict) else None
        if not c:
            continue
        await emit("link_followed", LinkFollowedEvent(url=proxy_doc.url, **{"from": parent.url}))
        # Only keep links where the crawl produced a genuine page title; a
        # URL-derived title (via _title_from_url) with no real content is a
        # pseudo-result, not worth ranking or indexing. The extract may be empty.
        crawled_title = (c.get("title") or "").strip()
        if proxy_doc.url and crawled_title:
            all_docs.append(Document(
                title=crawled_title,
                url=proxy_doc.url,
                extract=c.get("extract") or "",
            ))

    await _emit_final_results(query, all_docs, emit, last_results_key, lock)


async def _emit_final_results(
    query: str, all_docs: list[Document], emit, last_results_key: list, lock: asyncio.Lock
) -> None:
    terms = tokenize(query)
    final_limit = getattr(settings, "SUPER_SEARCH_FINAL_RESULTS_LIMIT", 100)

    # Serialize the score → dedup-check → emit sequence: concurrent secondary
    # tasks would otherwise both pass the dedup check before either updates the
    # key, emitting duplicate identical `results` frames and re-scoring the same
    # set in parallel.
    async with lock:
        seen: set[str] = set()
        unique: list[Document] = []
        for doc in all_docs:
            if doc.url and doc.title and doc.url not in seen:
                seen.add(doc.url)
                unique.append(doc)

        if terms:
            unique = [doc for doc in unique if _doc_passes_term_filter(doc, terms)]

        if not unique:
            return

        final_scores = await asyncio.to_thread(score_documents, ltr_model, query, unique)
        ranked = sorted(zip(unique, final_scores), key=lambda x: -x[1])[:final_limit]
        # Diversify with MMR (demotes, never drops, same-domain / near-duplicate
        # results) to match standard search — see MMRRanker in search_setup.py.
        score_by_url = {doc.url: score for doc, score in ranked}
        diversified = mmr_rerank([doc for doc, _ in ranked])
        ranked = [(doc, score_by_url[doc.url]) for doc in diversified]
        key = tuple(doc.url for doc, _ in ranked)
        if key == last_results_key[0]:
            return
        last_results_key[0] = key
        await emit("results", ResultsEvent(
            results=[_result_payload(doc, score, "", "final") for doc, score in ranked],
            count=len(ranked),
        ))


async def _index_results(query: str, docs: list[Document]) -> int:
    """Index everything Super Search found against the query's unigrams/bigrams.

    Runs the blocking index write off the event loop and never lets an indexing
    failure break the response. Returns the number of distinct new pages indexed.
    """
    if not docs:
        return 0
    try:
        return await asyncio.to_thread(
            index_results_against_query, docs, query, str(index_path)
        )
    except Exception:
        logger.exception("super-search failed to index results")
        return 0


async def _record_rewards(query: str, ctx: SelectionContext, last_results_key: list) -> None:
    """Compute implicit per-source rewards from the final top-K and log the impression."""
    if not ctx.selected:
        return
    final_urls = list(last_results_key[0] or ())
    top_k = getattr(settings, "SUPER_SEARCH_TOP_K", 10)
    rewards = compute_rewards(ctx, final_urls[:top_k])
    try:
        await asyncio.to_thread(log_impression, query, ctx, rewards)
    except Exception:
        logger.exception("super-search failed to record rewards")
    if getattr(settings, "SUPER_SEARCH_USE_BANDIT", False):
        try:
            await asyncio.to_thread(ss_bandit.update, ctx.features, rewards)
        except Exception:
            logger.exception("super-search failed to update bandit")


async def _run_pipeline(
    query: str, emit, all_docs: list[Document], last_results_key: list, lock: asyncio.Lock,
    ctx: SelectionContext,
) -> None:
    per_source_limit = settings.SUPER_SEARCH_RESULTS_PER_SOURCE
    ctx.per_source_limit = per_source_limit
    top_k = getattr(settings, "SUPER_SEARCH_TOP_K", 10)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    timeout = httpx.Timeout(settings.SUPER_SEARCH_PER_SOURCE_TIMEOUT)

    # Min-heap tracking top-K seen so far: (score, counter, doc).
    # A doc enters the top-K when the heap has < k entries or its score beats
    # the current minimum; once promoted it is never un-promoted.
    _heap: list[tuple[float, int, Document]] = []
    _promoted_urls: set[str] = set()
    _ctr = 0

    def _maybe_promote(doc: Document, score: float) -> bool:
        nonlocal _ctr
        if doc.url in _promoted_urls:
            return False
        if len(_heap) < top_k:
            heapq.heappush(_heap, (score, _ctr, doc))
        elif score > _heap[0][0]:
            heapq.heapreplace(_heap, (score, _ctr, doc))
        else:
            return False
        _ctr += 1
        _promoted_urls.add(doc.url)
        return True

    async with httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": HTTP_USER_AGENT},
    ) as client:
        # Select a subset of sources to query (cosine/bandit policy) rather than
        # fanning out to every registered source.
        sources_to_query = getattr(settings, "SUPER_SEARCH_SOURCES_TO_QUERY", len(SOURCES))
        ctx.candidates = list(SOURCES.keys())
        selected = await asyncio.to_thread(
            select_sources, query, ctx.candidates, sources_to_query, ctx
        )
        ctx.selected = selected

        source_tasks = []
        for name in selected:
            await emit("source_started", SourceStartedEvent(source=name))
            source_tasks.append(
                asyncio.create_task(_call_source(name, SOURCES[name], client, query, per_source_limit))
            )

        secondary: list[asyncio.Task] = []

        for completed in asyncio.as_completed(source_tasks):
            name, docs, error = await completed
            if error is not None:
                await emit("source_failed", SourceFailedEvent(source=name, error=error))
                continue
            await emit("source_returned", SourceReturnedEvent(source=name, count=len(docs)))
            if not docs:
                continue

            # Fold this source's results into its decaying-mean content profile
            # (best-effort; tracked in `secondary` so failures are logged at gather).
            secondary.append(asyncio.create_task(_update_profile(name, docs)))
            # Remember which source produced each URL, for reward attribution.
            ctx.record_results(name, [d.url for d in docs if d.url])

            scores = await asyncio.to_thread(_heuristic_score_docs, query, docs)
            for doc, score in zip(docs, scores):
                if doc.url and doc.title:
                    all_docs.append(doc)
                if _maybe_promote(doc, score):
                    await emit("result_promoted", _result_payload(doc, score, name, "direct"))
                    secondary.append(
                        asyncio.create_task(_follow_links(doc, query, emit, all_docs, last_results_key, lock))
                    )
            if all_docs:
                await _emit_final_results(query, all_docs, emit, last_results_key, lock)

        if secondary:
            results = await asyncio.gather(*secondary, return_exceptions=True)
            for exc in results:
                if isinstance(exc, Exception):
                    logger.exception("super-search secondary task failed", exc_info=exc)


# ---------------------------------------------------------------------------
# SSE generator
# ---------------------------------------------------------------------------

async def _sse_stream(query: str, monthly_usage: int, monthly_limit: int):
    queue: asyncio.Queue = asyncio.Queue()
    started = time.monotonic()
    reason = "complete"
    pages_indexed = 0
    all_docs: list[Document] = []
    last_results_key: list = [None]
    results_lock = asyncio.Lock()
    selection_ctx = SelectionContext()

    async def emit(event_type: str, data: Any) -> None:
        await queue.put((event_type, data))

    async def producer():
        nonlocal reason, pages_indexed
        try:
            await asyncio.wait_for(
                _run_pipeline(query, emit, all_docs, last_results_key, results_lock, selection_ctx),
                timeout=settings.SUPER_SEARCH_DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            reason = "timed_out"
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("super-search pipeline crashed")
            reason = "error"
            await queue.put(("error", ErrorEvent(message=str(e))))
        finally:
            if reason in ("complete", "timed_out"):
                try:
                    await _emit_final_results(query, all_docs, emit, last_results_key, results_lock)
                except Exception:
                    logger.exception("super-search failed to emit final results")
                pages_indexed = await _index_results(query, all_docs)
                await _record_rewards(query, selection_ctx, last_results_key)
            await queue.put(_SENTINEL)

    task = asyncio.create_task(producer())

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            if item is _SENTINEL:
                break
            event_type, data = item
            yield _sse_frame(event_type, data)

        yield _sse_frame("done", DoneEvent(
            reason=reason,
            elapsed_seconds=round(time.monotonic() - started, 3),
            monthly_usage=monthly_usage,
            monthly_limit=monthly_limit,
            pages_indexed=pages_indexed,
        ))
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def init_router() -> None:
    @router.get(
        "",
        auth=None,  # handled manually in the view to support both API key and JWT under an async view
        summary="Super Search (streaming, SSE)",
        description=(
            "Multi-source streaming search. Fans out to Mwmbl, Hacker News, "
            "GitHub, Stack Exchange, ArXiv and PyPI, re-ranks all results with "
            "the Mwmbl LTR model, and for items above the relevance threshold "
            "crawls the page and follows the most promising outbound links.\n\n"
            "Authentication is required (search-scoped API key in `X-API-Key` "
            "or JWT bearer token). A per-user monthly quota applies; the limit "
            "and your current usage are reported in the final `done` event.\n\n"
            "## Response\n\n"
            "Returns `text/event-stream`. Each event is framed as "
            "`event: <type>\\ndata: <json>\\n\\n`, where `<json>` is the payload "
            "described below. While the pipeline is idle the server sends SSE "
            "comment lines (`: keepalive\\n\\n`, no event/data) to keep the "
            "connection open; clients should ignore them.\n\n"
            "### Event types\n\n"
            "| Event | `data` payload | Meaning |\n"
            "|---|---|---|\n"
            "| `source_started` | `{source}` | A source's query task was launched (one per source). |\n"
            "| `source_returned` | `{source, count}` | A source finished; `count` is the number of raw results it returned. |\n"
            "| `source_failed` | `{source, error}` | A source errored or timed out; `error` is `\"timeout\"` or an exception message. |\n"
            "| `result_promoted` | result item (`origin=\"direct\"`) | A result entered the live top-K and will have its outbound links followed. |\n"
            "| `page_fetched` | `{url, links}` | A promoted page was crawled; `links` is the number of outbound links found. |\n"
            "| `link_followed` | `{url, from}` | An outbound link from a crawled page was fetched and added to the candidate pool. |\n"
            "| `results` | `{results[], count}` | The current authoritative ranking (items have `origin=\"final\"`). Emitted progressively after each source and once more at the end — **replace** your displayed list on each. |\n"
            "| `error` | `{message}` | The pipeline crashed; the stream ends. |\n"
            "| `done` | `{reason, elapsed_seconds, monthly_usage, monthly_limit, pages_indexed}` | Terminal event. `reason` is `complete`, `timed_out`, `cancelled` or `error`; `pages_indexed` is the number of new pages added to the Mwmbl index by this search. |\n\n"
            "The exact JSON shape of every payload is given by the `oneOf` schema "
            "of the 200 response below."
        ),
        openapi_extra={
            "parameters": [{
                "name": "q",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "example": "rust async runtimes"},
            }],
            "responses": {
                "200": {
                    "description": (
                        "An SSE stream of search events. The body is a "
                        "`text/event-stream`; each event's `data:` line is a JSON "
                        "object matching one of the schemas below (keyed by event "
                        "type via the schema `title`)."
                    ),
                    "content": {
                        "text/event-stream": {"schema": {"oneOf": _event_oneof()}},
                    },
                },
            },
        },
    )
    async def super_search(request, q: str):
        user = await authenticate_user(request)

        if not await sync_to_async(check_rate_limit)(user.id):
            raise HttpError(429, "Rate limit exceeded: maximum 5 requests per second.")

        monthly_limit = settings.SUPER_SEARCH_MONTHLY_LIMIT
        # Increment first, then check: this makes the quota check atomic under
        # concurrent requests (a check-then-increment would let racing requests
        # both pass). Refund the increment if the caller is over the limit.
        monthly_usage = await sync_to_async(increment_monthly_super_search)(user.id)
        if monthly_usage > monthly_limit:
            await sync_to_async(decrement_monthly_super_search)(user.id)
            raise HttpError(
                429,
                f"Super Search monthly quota exceeded: {monthly_limit} requests per month "
                f"and you have used {monthly_limit}.",
            )

        return StreamingHttpResponse(
            _sse_stream(q, monthly_usage, monthly_limit),
            content_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-cache",
            },
        )
