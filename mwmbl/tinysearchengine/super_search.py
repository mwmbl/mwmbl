"""Super Search — multi-source streaming search endpoint.

Fans out a query to several external search APIs, re-ranks the union with the
existing LTR model, and for any result above the threshold fetches the page,
picks promising outbound links, and re-ranks those too. Progress and results
are streamed to the client over Server-Sent Events.

See plan: /api/v2/super-search/
"""
import asyncio
import heapq
import json
import logging
import re
import time
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import StreamingHttpResponse
from ninja import Router
from ninja.errors import HttpError

from mwmbl.crawler.retrieve import crawl_url
from mwmbl.models import MwmblUser
from mwmbl.quota import (
    check_rate_limit,
    get_monthly_super_search_count,
    increment_monthly_super_search,
)
from mwmbl.search_auth import SearchApiKeyAuth
from mwmbl.search_setup import ltr_model
from mwmbl.tinysearchengine.indexer import Document
from mwmbl.tinysearchengine.ltr_rank import score_documents
from mwmbl.tinysearchengine.rank import get_features
from mwmbl.tinysearchengine.super_search_sources import SOURCES
from mwmbl.tokenizer import tokenize

logger = logging.getLogger(__name__)

router = Router(tags=["Super Search"])

KEEPALIVE_INTERVAL = 5.0  # seconds between idle keepalive comments
HTTP_USER_AGENT = "mwmbl-super-search/0.1 (+https://mwmbl.org)"
_SENTINEL: object = object()
_URL_EXT_RE = re.compile(r"\.\w{1,5}$")
_URL_TOKEN_RE = re.compile(r"[-_+]+")


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse_frame(event_type: str, data: Any) -> bytes:
    payload = json.dumps(data, default=str, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


def _result_payload(doc: Document, score: float, source: str, origin: str) -> dict:
    return {
        "url": doc.url,
        "title": doc.title,
        "extract": doc.extract,
        "score": round(score, 4),
        "source": source,
        "origin": origin,
    }


def _doc_passes_term_filter(doc: Document, terms: list[str]) -> bool:
    """Return True if more than half the query terms match somewhere in the document.

    Mirrors the filter in rank.py (score_result) to avoid surfacing results with
    no meaningful term overlap before the LTR model sees them.
    """
    if not terms:
        return True
    features = get_features(
        terms,
        doc.title or "",
        doc.url or "",
        doc.extract or "",
        doc.score or 0.0,
        True,
    )
    return features["match_terms"] > len(terms) / 2


def _url_term_score(url: str, terms: list[str]) -> int:
    """Count how many query terms appear anywhere in the URL (case-insensitive).

    Used to rank outbound links before crawling them — the LTR model does poorly
    with proxy docs that have only URL-derived titles, while simple term overlap
    directly captures relevance signal available in the URL itself.
    """
    url_lower = url.lower()
    return sum(1 for t in terms if t in url_lower)


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



async def _authenticate(request) -> MwmblUser:
    """Resolve the requesting user from either an X-API-Key header or a JWT.

    Returns the user on success; raises HttpError(401) otherwise.
    Database lookups are off-loaded via ``sync_to_async`` because the view
    that calls this is async.
    """
    raw_key = request.headers.get("X-API-Key")
    if raw_key:
        api_key = await sync_to_async(SearchApiKeyAuth().authenticate)(request, raw_key)
        if api_key is None:
            raise HttpError(401, "Invalid API key.")
        return await sync_to_async(lambda: api_key.user)()

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):]
        try:
            from ninja_jwt.tokens import AccessToken
            access = AccessToken(token)
            user_id = access["user_id"]
        except Exception:
            raise HttpError(401, "Invalid token.")
        try:
            return await sync_to_async(MwmblUser.objects.get)(id=user_id)
        except MwmblUser.DoesNotExist:
            raise HttpError(401, "Unknown user.")

    raise HttpError(401, "Authentication required: X-API-Key or Bearer token.")


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
    parent: Document, parent_source: str, query: str, emit, all_docs: list[Document]
) -> None:
    """Crawl the parent URL, score its outbound links, and collect the best for final ranking."""
    max_links = settings.SUPER_SEARCH_MAX_LINKS_PER_PAGE

    try:
        result = await asyncio.to_thread(crawl_url, parent.url)
    except Exception as e:  # noqa: BLE001
        logger.info("crawl_url failed for %s: %s", parent.url, e)
        return

    content = result.get("content") if isinstance(result, dict) else None
    if not content:
        return

    raw_links = list(content.get("links") or []) + list(content.get("extra_links") or [])
    raw_links = raw_links[:50]
    await emit("page_fetched", {"url": parent.url, "links": len(raw_links)})
    if not raw_links:
        return

    terms = tokenize(query)
    proxy_docs = [Document(title=_title_from_url(u), url=u, extract="") for u in raw_links]
    proxy_scores = [_url_term_score(d.url, terms) for d in proxy_docs]

    ranked = sorted(zip(proxy_docs, proxy_scores), key=lambda x: -x[1])[:max_links]
    if not ranked:
        return

    fetches = await asyncio.gather(
        *[asyncio.to_thread(crawl_url, d.url) for d, _ in ranked],
        return_exceptions=True,
    )

    for (proxy_doc, _), fetched in zip(ranked, fetches):
        if isinstance(fetched, Exception):
            continue
        c = fetched.get("content") if isinstance(fetched, dict) else None
        if not c:
            continue
        await emit("link_followed", {"url": proxy_doc.url, "from": parent.url})
        doc = Document(
            title=c.get("title") or _title_from_url(proxy_doc.url),
            url=proxy_doc.url,
            extract=c.get("extract") or "",
        )
        if doc.url and doc.title and doc.extract:
            all_docs.append(doc)


async def _run_pipeline(query: str, emit) -> None:
    per_source_limit = settings.SUPER_SEARCH_RESULTS_PER_SOURCE
    top_k = getattr(settings, "SUPER_SEARCH_TOP_K", 10)
    terms = tokenize(query)
    final_limit = getattr(settings, "SUPER_SEARCH_FINAL_RESULTS_LIMIT", 100)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    timeout = httpx.Timeout(settings.SUPER_SEARCH_PER_SOURCE_TIMEOUT)

    all_docs: list[Document] = []

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
        source_tasks = []
        for name, fn in SOURCES.items():
            await emit("source_started", {"source": name})
            source_tasks.append(
                asyncio.create_task(_call_source(name, fn, client, query, per_source_limit))
            )

        secondary: list[asyncio.Task] = []

        for completed in asyncio.as_completed(source_tasks):
            name, docs, error = await completed
            if error is not None:
                await emit("source_failed", {"source": name, "error": error})
                continue
            await emit("source_returned", {"source": name, "count": len(docs)})
            if not docs:
                continue

            scores = await asyncio.to_thread(score_documents, ltr_model, query, docs)
            for doc, score in zip(docs, scores):
                if doc.url and doc.title and doc.extract:
                    all_docs.append(doc)
                if _maybe_promote(doc, score):
                    await emit("result_promoted", _result_payload(doc, score, name, "direct"))
                    secondary.append(
                        asyncio.create_task(_follow_links(doc, name, query, emit, all_docs))
                    )

        if secondary:
            await asyncio.gather(*secondary, return_exceptions=True)

    # Deduplicate and produce final ranked list.
    seen: set[str] = set()
    unique: list[Document] = []
    for doc in all_docs:
        if doc.url and doc.title and doc.extract and doc.url not in seen:
            seen.add(doc.url)
            unique.append(doc)

    if terms:
        unique = [doc for doc in unique if _doc_passes_term_filter(doc, terms)]

    if unique:
        final_scores = await asyncio.to_thread(score_documents, ltr_model, query, unique)
        ranked = sorted(zip(unique, final_scores), key=lambda x: -x[1])[:final_limit]
        await emit("results", {
            "results": [_result_payload(doc, score, "", "final") for doc, score in ranked],
            "count": len(ranked),
        })


# ---------------------------------------------------------------------------
# SSE generator
# ---------------------------------------------------------------------------

async def _sse_stream(query: str, monthly_usage: int, monthly_limit: int):
    queue: asyncio.Queue = asyncio.Queue()
    started = time.monotonic()
    reason = "complete"

    async def emit(event_type: str, data: Any) -> None:
        await queue.put((event_type, data))

    async def producer():
        nonlocal reason
        try:
            await asyncio.wait_for(
                _run_pipeline(query, emit),
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
            await queue.put(("error", {"message": str(e)}))
        finally:
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

        yield _sse_frame("done", {
            "reason": reason,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "monthly_usage": monthly_usage,
            "monthly_limit": monthly_limit,
        })
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
            "Multi-source streaming search. Fans out to Hacker News, GitHub, "
            "Stack Exchange, ArXiv and PyPI, re-ranks all results with the "
            "Mwmbl LTR model, and for items above the relevance threshold "
            "crawls the page and follows the most promising outbound links.\n\n"
            "Authentication is required (search-scoped API key in `X-API-Key` "
            "or JWT bearer token). Quota is 10 requests per user per month.\n\n"
            "Returns `text/event-stream`. Event types: `source_started`, "
            "`source_returned`, `source_failed`, `result_promoted`, "
            "`page_fetched`, `link_followed`, `error`, `done`."
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
                    "description": "An SSE stream of search events.",
                    "content": {"text/event-stream": {"schema": {"type": "string"}}},
                },
            },
        },
    )
    async def super_search(request, q: str):
        user = await _authenticate(request)

        if not await sync_to_async(check_rate_limit)(user.id):
            raise HttpError(429, "Rate limit exceeded: maximum 5 requests per second.")

        monthly_limit = settings.SUPER_SEARCH_MONTHLY_LIMIT
        current = await sync_to_async(get_monthly_super_search_count)(user.id)
        if current >= monthly_limit:
            raise HttpError(
                429,
                f"Super Search monthly quota exceeded: {monthly_limit} requests per month "
                f"and you have used {current}.",
            )
        monthly_usage = await sync_to_async(increment_monthly_super_search)(user.id)

        return StreamingHttpResponse(
            _sse_stream(q, monthly_usage, monthly_limit),
            content_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-cache",
            },
        )
