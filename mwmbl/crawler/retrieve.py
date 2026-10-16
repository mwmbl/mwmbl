import json
import random
import re
import ssl
import threading
import time
from http.cookiejar import DefaultCookiePolicy
from logging import getLogger
from multiprocessing.pool import ThreadPool
from ssl import SSLCertVerificationError
from typing import List, Optional
from urllib.parse import urljoin, urlparse, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from redis import Redis
from requests import ReadTimeout
from requests.adapters import HTTPAdapter
from requests.utils import DEFAULT_CA_BUNDLE_PATH
from urllib3.exceptions import MaxRetryError, NewConnectionError

from mwmbl.crawler.env_vars import MWMBL_CONTACT_INFO
from mwmbl.crawler.ssrf import UnsafeURLError, validate_url
from mwmbl.justext import core, utils
from mwmbl.justext.core import html_to_dom
from mwmbl.justext.paragraph import Paragraph

# UnsafeURLError subclasses ValueError, so it is already covered here, but list
# it explicitly for clarity: an SSRF-blocked URL is skipped like any other bad URL.
ALLOWED_EXCEPTIONS = (
    ValueError,
    UnsafeURLError,
    ConnectionError,
    ReadTimeout,
    TimeoutError,
    OSError,
    NewConnectionError,
    MaxRetryError,
    SSLCertVerificationError,
)

TIMEOUT_SECONDS = 3
MAX_REDIRECTS = 5
MAX_FETCH_SIZE = 1024 * 1024
MAX_URL_LENGTH = 150
BAD_URL_REGEX = re.compile(r"\/\/localhost\b|\.jpg$|\.png$|\.js$|\.gz$|\.zip$|\.pdf$|\.bz2$|\.ipynb$|\.py$")
MAX_NEW_LINKS = 50
MAX_EXTRA_LINKS = 50
NUM_TITLE_CHARS = 65
NUM_EXTRACT_CHARS = 155
# Fallback extract (first-paragraph) tuning: ignore paragraphs shorter than this
# or whose text is mostly link anchors (nav/breadcrumbs rather than prose).
MIN_FALLBACK_PARAGRAPH_CHARS = 20
MAX_FALLBACK_PARAGRAPH_LINK_DENSITY = 0.5
DEFAULT_ENCODING = "utf8"
DEFAULT_ENC_ERRORS = "replace"
MAX_SITE_URLS = 100
CRAWLER_VERSION: str = "0.2.0"
USER_AGENT = f"mwmbl/{CRAWLER_VERSION} (https://github.com/mwmbl/mwmbl/ contact {MWMBL_CONTACT_INFO})"

ROBOTS_CACHE_TTL_SECONDS = 60 * 60 * 24
ROBOTS_CACHE_ERROR_TTL_SECONDS = 60 * 60

# Diagnostic: how often a fetch starts while another fetch to the same domain is in flight,
# across every worker sharing this Redis. Read the totals with HGETALL crawl-domain-overlap.
DOMAIN_IN_FLIGHT_KEY = "crawl-in-flight-{domain}"
DOMAIN_OVERLAP_KEY = "crawl-domain-overlap"
# Only matters if a worker dies mid-crawl and never decrements: the stale count then
# lingers until the domain goes this long without a fetch starting.
DOMAIN_IN_FLIGHT_EXPIRY_SECONDS = 120

logger = getLogger(__name__)

# One verifying context for every fetch. Left to itself, requests builds a fresh context for
# each new connection and parses the whole CA bundle into it, which on OpenSSL 3 costs about
# 100ms of CPU - most of what the crawler spent per page. requests no longer shares a
# preloaded context, so the crawler has to.
SSL_CONTEXT = ssl.create_default_context(cafile=DEFAULT_CA_BUNDLE_PATH)

_thread_local = threading.local()


class SharedContextAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block, ssl_context=SSL_CONTEXT, **pool_kwargs)

    def cert_verify(self, conn, url, verify, cert):
        """Keep verification on, but stop urllib3 reloading the bundle into SSL_CONTEXT.

        The base class points every connection at the bundle file, and urllib3 then calls
        load_verify_locations on the shared context per connection - the cost the shared
        context exists to avoid. SSL_CONTEXT was built from that same bundle.
        """
        super().cert_verify(conn, url, verify, cert)
        conn.ca_certs = None
        conn.ca_cert_dir = None


def get_session() -> requests.Session:
    """A session per thread: requests does not promise a Session is safe to share.

    Keeping it also keeps connections alive between fetches, so a page's robots.txt and the
    page itself share one TLS handshake.
    """
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        # Otherwise every request scans os.environ for proxy and CA bundle settings.
        session.trust_env = False
        # The session outlives the fetch - for ever, on Super Search's long-lived executor - so
        # a stored cookie would grow without bound and be sent on other users' fetches. An
        # empty allowlist refuses every cookie, as a fresh requests.get per fetch did.
        session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        session.mount("https://", SharedContextAdapter())
        _thread_local.session = session
    return _thread_local.session


def fetch(url):
    """
    Fetch with a maximum timeout and maximum fetch size to avoid big pages bringing us down.

    Redirects are followed manually so each hop can be re-validated against the
    SSRF guard: a public URL must not be able to 3xx us into an internal address.

    Returns (status code, content, resolved URL). The resolved URL is the last hop, which is
    the only place the redirect chain is visible to a caller: it says which host actually
    served the page, rather than which one we asked.

    https://stackoverflow.com/a/22347526
    """

    headers = {"User-Agent": USER_AGENT}
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(url)
        r = get_session().get(url, stream=True, timeout=TIMEOUT_SECONDS, headers=headers, allow_redirects=False)

        if r.is_redirect and r.next is not None:
            r.close()
            url = urljoin(url, r.headers.get("Location", ""))
            continue

        size = 0
        start = time.time()

        content = b""
        for chunk in r.iter_content(1024):
            if time.time() - start > TIMEOUT_SECONDS:
                raise ValueError("Timeout reached")

            content += chunk

            size += len(chunk)
            if size > MAX_FETCH_SIZE:
                logger.debug(f"Maximum size reached for URL {url}")
                break

        # A response left open holds its connection out of the session's pool.
        r.close()
        return r.status_code, content, url

    raise ValueError(f"Too many redirects for URL {url}")


def robots_allowed(url: str, redis: Redis) -> bool:
    try:
        parsed_url = urlparse(url)
    except ValueError:
        logger.info(f"Unable to parse URL: {url}")
        return False

    domain = parsed_url.netloc
    robots_url = urlunsplit((parsed_url.scheme, parsed_url.netloc, "robots.txt", "", ""))

    cached_content = _get_robots_from_cache(redis, domain)
    if cached_content is not None:
        logger.debug(f"Robots cache hit for {domain}")
        parse_robots = RobotFileParser()
        parse_robots.parse(cached_content)
        allowed = parse_robots.can_fetch(USER_AGENT, url)
        logger.debug(f"Robots allowed for {url} (cached): {allowed}")
        return allowed

    try:
        status_code, content, _ = fetch(robots_url)
    except ALLOWED_EXCEPTIONS as e:
        logger.debug(f"Robots error: {robots_url}, {e}")
        _cache_robots_content(redis, domain, [], error=True)
        return True

    if status_code != 200:
        logger.debug(f"Robots status code: {status_code}")
        _cache_robots_content(redis, domain, [], error=True)
        return True

    decoded = None
    for encoding in ["utf-8", "iso-8859-1"]:
        try:
            decoded = content.decode(encoding).splitlines()
            break
        except UnicodeDecodeError:
            pass

    if decoded is None:
        logger.info(f"Unable to decode robots file {robots_url}")
        _cache_robots_content(redis, domain, [], error=True)
        return True

    parse_robots = RobotFileParser()
    parse_robots.parse(decoded)
    allowed = parse_robots.can_fetch(USER_AGENT, url)

    _cache_robots_content(redis, domain, decoded, error=False)

    logger.debug(f"Robots allowed for {url}: {allowed}")
    return allowed


def _get_robots_from_cache(redis: Redis, domain: str) -> Optional[List[str]]:
    cache_key = f"robots:{domain}"
    cached = redis.get(cache_key)
    if cached is None:
        return None

    try:
        data = json.loads(cached)
        if time.time() > data["expires_at"]:
            redis.delete(cache_key)
            return None
        return data["content"]
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Invalid cache entry for {cache_key}: {e}, removing")
        redis.delete(cache_key)
        return None


def _cache_robots_content(redis: Redis, domain: str, content: List[str], error: bool = False):
    cache_key = f"robots:{domain}"
    now = time.time()
    ttl = ROBOTS_CACHE_ERROR_TTL_SECONDS if error else ROBOTS_CACHE_TTL_SECONDS

    cache_data = {"content": content, "cached_at": int(now), "expires_at": int(now + ttl)}

    redis.setex(cache_key, ttl, json.dumps(cache_data))
    logger.debug(f"Cached robots.txt for {domain}, expires in {ttl}s")


def _resolve_and_validate_link(href: str, current_url: str) -> str | None:
    """Resolve a raw href to an absolute URL and validate it. Returns None if invalid."""
    href = urljoin(current_url, href)
    if not href.startswith("http") or len(href) > MAX_URL_LENGTH:
        return None
    if BAD_URL_REGEX.search(href):
        return None
    try:
        parsed = urlparse(href)
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def get_dom_links(dom, current_url: str) -> set[str]:
    """Extract hrefs from all <a> elements in the DOM.

    justext drops paragraphs with no text nodes (e.g. icon-only anchors like
    <a href="/discord"><img .../></a>), so their links never reach get_new_links.
    This function captures those missed hrefs directly via XPath.
    """
    result = set()
    for anchor in dom.xpath("//a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        resolved = _resolve_and_validate_link(href, current_url)
        if resolved:
            result.add(resolved)
    return result


def get_new_links(paragraphs: list[Paragraph], current_url):
    new_links = set()
    extra_links = set()

    for paragraph in paragraphs:
        if len(paragraph.links) > 0:
            for link in paragraph.links:
                resolved = _resolve_and_validate_link(link, current_url)
                if resolved is None:
                    logger.debug(f"Bad URL: {link}")
                    continue
                if paragraph.class_type == "good":
                    if len(new_links) < MAX_NEW_LINKS:
                        new_links.add(resolved)
                else:
                    if len(extra_links) < MAX_EXTRA_LINKS and resolved not in new_links:
                        extra_links.add(resolved)
                if len(new_links) >= MAX_NEW_LINKS and len(extra_links) >= MAX_EXTRA_LINKS:
                    return new_links, extra_links
    return new_links, extra_links


def extract_from_html_text(html_text: str) -> str:
    """Extract a clean plain-text snippet from an HTML fragment using the justext pipeline."""
    html_bytes = f"<html><body>{html_text}</body></html>".encode(DEFAULT_ENCODING)
    try:
        dom = html_to_dom(html_bytes, DEFAULT_ENCODING, None, DEFAULT_ENC_ERRORS)
        paragraphs = core.justext_from_dom(dom, utils.get_stoplist("English"))
    except Exception:
        return ""
    extract = ""
    for paragraph in paragraphs:
        if paragraph.class_type != "good":
            continue
        extract += " " + paragraph.text.strip()
        if len(extract) > NUM_EXTRACT_CHARS:
            extract = extract[: NUM_EXTRACT_CHARS - 1] + "…"
            break
    return extract.strip()


def get_og_meta(dom) -> tuple[str, str]:
    """Return (og:title, og:description) from Open Graph meta tags, or empty strings."""
    og_title = ""
    og_desc = ""
    for meta in dom.xpath("//meta[@property and @content]"):
        prop = (meta.get("property") or "").strip().lower()
        value = (meta.get("content") or "").strip()
        if prop == "og:title" and not og_title:
            og_title = value
        elif prop == "og:description" and not og_desc:
            og_desc = value
        if og_title and og_desc:
            break
    return og_title, og_desc


def get_meta_description(dom) -> str:
    """Return the <meta name="description"> content, or empty string."""
    for meta in dom.xpath("//meta[@name and @content]"):
        if (meta.get("name") or "").strip().lower() == "description":
            return (meta.get("content") or "").strip()
    return ""


def get_first_paragraph(dom) -> str:
    """Return the text of the first substantive <p> element, or empty string.

    A last-resort fallback for pages where justext finds no 'good' content — most
    commonly non-English pages, where the English stoplist gives ~0 stopword
    density so genuine prose is classified as boilerplate. Paragraphs that are too
    short or dominated by link text (nav/breadcrumbs) are skipped so the snippet
    is real body text.
    """
    for p in dom.xpath("//p"):
        text = " ".join(" ".join(p.itertext()).split())
        if len(text) < MIN_FALLBACK_PARAGRAPH_CHARS:
            continue
        link_text = " ".join(" ".join(a.itertext()) for a in p.xpath(".//a"))
        link_chars = len(" ".join(link_text.split()))
        if link_chars > len(text) * MAX_FALLBACK_PARAGRAPH_LINK_DENSITY:
            continue
        return text
    return ""


def _truncate(text: str, limit: int) -> str:
    return text[: limit - 1] + "…" if len(text) > limit else text


def crawl_url(url, redis: Redis):
    logger.info(url)
    js_timestamp = int(time.time() * 1000)
    allowed = robots_allowed(url, redis)
    if not allowed:
        return {
            "url": url,
            "status": None,
            "timestamp": js_timestamp,
            "content": None,
            "error": {
                "name": "RobotsDenied",
                "message": "Robots do not allow this URL",
            },
        }

    try:
        status_code, content, resolved_url = fetch(url)
    except ALLOWED_EXCEPTIONS as e:
        logger.debug(f"Exception crawling URl {url}: {e}")
        return {
            "url": url,
            "status": None,
            "timestamp": js_timestamp,
            "content": None,
            "error": {
                "name": "AbortError",
                "message": str(e),
            },
        }

    if len(content) == 0:
        return {
            "url": url,
            "resolved_url": resolved_url,
            "status": status_code,
            "timestamp": js_timestamp,
            "content": None,
            "error": {
                "name": "NoResponseText",
                "message": "No response found",
            },
        }

    content = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", content)

    try:
        dom = html_to_dom(content, DEFAULT_ENCODING, None, DEFAULT_ENC_ERRORS)
    except Exception as e:
        logger.exception(f"Error parsing dom: {url}")
        return {
            "url": url,
            "resolved_url": resolved_url,
            "status": status_code,
            "timestamp": js_timestamp,
            "content": None,
            "error": {
                "name": e.__class__.__name__,
                "message": str(e),
            },
        }

    title_element = dom.xpath("//title")
    title = ""
    if len(title_element) > 0:
        title_text = title_element[0].text
        if title_text is not None:
            title = title_text.strip()

    if len(title) > NUM_TITLE_CHARS:
        title = title[: NUM_TITLE_CHARS - 1] + "…"

    try:
        paragraphs = core.justext_from_dom(dom, utils.get_stoplist("English"))
    except Exception as e:
        bad_bytes = sorted({b for b in content if b < 0x20 and b not in (0x09, 0x0A, 0x0D)})
        logger.exception("Error parsing paragraphs - offending control bytes: %s", bad_bytes)
        return {
            "url": url,
            "resolved_url": resolved_url,
            "status": status_code,
            "timestamp": js_timestamp,
            "content": None,
            "error": {
                "name": e.__class__.__name__,
                "message": str(e),
            },
        }

    new_links, extra_links = get_new_links(paragraphs, url)

    # Also capture links from image-only anchors (e.g. icon links) that justext
    # drops because their paragraphs have no text nodes.
    for link in get_dom_links(dom, url):
        if link not in new_links and len(extra_links) < MAX_EXTRA_LINKS:
            extra_links.add(link)

    logger.debug(f"Got new links {new_links}")
    logger.debug(f"Got extra links {extra_links}")

    extract = ""
    for paragraph in paragraphs:
        if paragraph.class_type != "good":
            continue
        extract += " " + paragraph.text.strip()
        if len(extract) > NUM_EXTRACT_CHARS:
            extract = extract[: NUM_EXTRACT_CHARS - 1] + "…"
            break

    # justext finds no body content for JS-first pages (e.g. Discord, SPAs) and
    # for non-English pages (the English stoplist classifies real prose as
    # boilerplate). Fall back, in order, to Open Graph tags, the meta description,
    # and finally the first substantive paragraph so these pages stay indexable.
    if not title or not extract:
        og_title, og_desc = get_og_meta(dom)
        if not title and og_title:
            title = _truncate(og_title, NUM_TITLE_CHARS)
        if not extract:
            fallback = og_desc or get_meta_description(dom) or get_first_paragraph(dom)
            if fallback:
                extract = _truncate(fallback, NUM_EXTRACT_CHARS)

    return {
        "url": url,
        "resolved_url": resolved_url,
        "status": status_code,
        "timestamp": js_timestamp,
        "content": {
            "title": title,
            "extract": extract,
            "links": sorted(new_links),
            "extra_links": sorted(extra_links),
        },
        "error": None,
    }


def crawl_batch(urls: list[str], num_threads: int, delay_seconds: float, redis: Redis) -> list[dict]:
    """Crawl a batch concurrently, returning results in the order of the URLs.

    Fetching is almost all waiting on the network, so one URL at a time leaves the machine
    idle. The queue hands out at most one URL per domain in a batch, so crawling a batch in
    parallel does not put more load on any one site.

    Each thread waits delay_seconds, with 10% random fuzz, between the URLs it crawls.
    """

    overlaps = []

    def crawl_after_delay(url: str) -> dict:
        if delay_seconds and getattr(_thread_local, "has_crawled", False):
            time.sleep(delay_seconds * (0.9 + 0.2 * random.random()))
        _thread_local.has_crawled = True
        result, overlap = crawl_counting_overlap(url, redis)
        overlaps.append(overlap)
        return result

    with ThreadPool(num_threads) as pool:
        results = pool.map(crawl_after_delay, urls)

    record_overlap(urls, overlaps, redis)
    return results


def crawl_counting_overlap(url: str, redis: Redis) -> tuple[dict, int]:
    """Crawl url, also returning how many other crawls of its domain were in flight as it started."""
    key = DOMAIN_IN_FLIGHT_KEY.format(domain=urlparse(url).netloc)
    in_flight, _ = redis.pipeline().incr(key).expire(key, DOMAIN_IN_FLIGHT_EXPIRY_SECONDS).execute()
    try:
        return crawl_url(url, redis), in_flight - 1
    finally:
        redis.decr(key)


def record_overlap(urls: list[str], overlaps: list[int], redis: Redis):
    """Add a batch to the overlap totals.

    duplicate_domains counts URLs whose domain already appeared earlier in the same batch, so
    overlapped minus duplicate_domains is a lower bound on the overlap between batches.
    """
    num_domains = len({urlparse(url).netloc for url in urls})
    duplicate_domains = len(urls) - num_domains
    overlapped = sum(1 for overlap in overlaps if overlap > 0)
    max_overlap = max(overlaps, default=0)
    logger.info(
        f"Domain overlap: {overlapped} of {len(urls)} fetches started while another fetch to the same "
        f"domain was in flight (max {max_overlap} others); {duplicate_domains} duplicate domains in batch"
    )
    redis.pipeline().hincrby(DOMAIN_OVERLAP_KEY, "fetches", len(urls)).hincrby(
        DOMAIN_OVERLAP_KEY, "overlapped", overlapped
    ).hincrby(DOMAIN_OVERLAP_KEY, "duplicate_domains", duplicate_domains).execute()
