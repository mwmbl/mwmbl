import re
import time
from logging import getLogger
from multiprocessing.pool import ThreadPool
from ssl import SSLCertVerificationError
from urllib.parse import urlparse, urlunsplit, urljoin
from urllib.robotparser import RobotFileParser

import requests
from mwmbl.justext import core, utils
from requests import ReadTimeout
from urllib3.exceptions import NewConnectionError, MaxRetryError

from mwmbl.crawler.env_vars import MWMBL_CONTACT_INFO
from mwmbl.crawler.ssrf import UnsafeURLError, validate_url
from mwmbl.justext.core import html_to_dom
from mwmbl.justext.paragraph import Paragraph


# UnsafeURLError subclasses ValueError, so it is already covered here, but list
# it explicitly for clarity: an SSRF-blocked URL is skipped like any other bad URL.
ALLOWED_EXCEPTIONS = (ValueError, UnsafeURLError, ConnectionError, ReadTimeout, TimeoutError,
                      OSError, NewConnectionError, MaxRetryError, SSLCertVerificationError)

POST_BATCH_URL = '/api/v1/crawler/batches/'
POST_NEW_BATCH_URL = '/api/v1/crawler/batches/new'

TIMEOUT_SECONDS = 3
MAX_REDIRECTS = 5
MAX_FETCH_SIZE = 1024*1024
MAX_URL_LENGTH = 150
BAD_URL_REGEX = re.compile(r'\/\/localhost\b|\.jpg$|\.png$|\.js$|\.gz$|\.zip$|\.pdf$|\.bz2$|\.ipynb$|\.py$')
MAX_NEW_LINKS = 50
MAX_EXTRA_LINKS = 50
NUM_TITLE_CHARS = 65
NUM_EXTRACT_CHARS = 155
DEFAULT_ENCODING = 'utf8'
DEFAULT_ENC_ERRORS = 'replace'
MAX_SITE_URLS = 100
CRAWLER_VERSION: str = "0.2.0"
USER_AGENT = f"mwmbl/{CRAWLER_VERSION} (https://github.com/mwmbl/mwmbl/ contact {MWMBL_CONTACT_INFO})"


logger = getLogger(__name__)


def fetch(url):
    """
    Fetch with a maximum timeout and maximum fetch size to avoid big pages bringing us down.

    Redirects are followed manually so each hop can be re-validated against the
    SSRF guard: a public URL must not be able to 3xx us into an internal address.

    https://stackoverflow.com/a/22347526
    """

    headers = {"User-Agent": USER_AGENT}
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(url)
        r = requests.get(url, stream=True, timeout=TIMEOUT_SECONDS,
                         headers=headers, allow_redirects=False)

        if r.is_redirect and r.next is not None:
            r.close()
            url = urljoin(url, r.headers.get("Location", ""))
            continue

        size = 0
        start = time.time()

        content = b""
        for chunk in r.iter_content(1024):
            if time.time() - start > TIMEOUT_SECONDS:
                raise ValueError('Timeout reached')

            content += chunk

            size += len(chunk)
            if size > MAX_FETCH_SIZE:
                logger.debug(f"Maximum size reached for URL {url}")
                break

        return r.status_code, content

    raise ValueError(f"Too many redirects for URL {url}")


def robots_allowed(url):
    try:
        parsed_url = urlparse(url)
    except ValueError:
        logger.info(f"Unable to parse URL: {url}")
        return False

    robots_url = urlunsplit((parsed_url.scheme, parsed_url.netloc, 'robots.txt', '', ''))

    parse_robots = RobotFileParser(robots_url)

    try:
        status_code, content = fetch(robots_url)
    except ALLOWED_EXCEPTIONS as e:
        logger.debug(f"Robots error: {robots_url}, {e}")
        return True

    if status_code != 200:
        logger.debug(f"Robots status code: {status_code}")
        return True

    decoded = None
    for encoding in ['utf-8', 'iso-8859-1']:
        try:
            decoded = content.decode(encoding).splitlines()
            break
        except UnicodeDecodeError:
            pass

    if decoded is None:
        logger.info(f"Unable to decode robots file {robots_url}")
        return True
    
    parse_robots.parse(decoded)
    allowed = parse_robots.can_fetch(USER_AGENT, url)
    logger.debug(f"Robots allowed for {url}: {allowed}")
    return allowed


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
                if paragraph.class_type == 'good':
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
            extract = extract[:NUM_EXTRACT_CHARS - 1] + "…"
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


def crawl_url(url):
    logger.info(url)
    js_timestamp = int(time.time() * 1000)
    allowed = robots_allowed(url)
    if not allowed:
        return {
            'url': url,
            'status': None,
            'timestamp': js_timestamp,
            'content': None,
            'error': {
                'name': 'RobotsDenied',
                'message': 'Robots do not allow this URL',
            }
        }

    try:
        status_code, content = fetch(url)
    except ALLOWED_EXCEPTIONS as e:
        logger.debug(f"Exception crawling URl {url}: {e}")
        return {
            'url': url,
            'status': None,
            'timestamp': js_timestamp,
            'content': None,
            'error': {
                'name': 'AbortError',
                'message': str(e),
            }
        }

    if len(content) == 0:
        return {
            'url': url,
            'status': status_code,
            'timestamp': js_timestamp,
            'content': None,
            'error': {
                'name': 'NoResponseText',
                'message': 'No response found',
            }
        }

    content = re.sub(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]', b'', content)

    try:
        dom = html_to_dom(content, DEFAULT_ENCODING, None, DEFAULT_ENC_ERRORS)
    except Exception as e:
        logger.exception(f"Error parsing dom: {url}")
        return {
            'url': url,
            'status': status_code,
            'timestamp': js_timestamp,
            'content': None,
            'error': {
                'name': e.__class__.__name__,
                'message': str(e),
            }
        }
        
    title_element = dom.xpath("//title")
    title = ""
    if len(title_element) > 0:
        title_text = title_element[0].text
        if title_text is not None:
            title = title_text.strip()

    if len(title) > NUM_TITLE_CHARS:
        title = title[:NUM_TITLE_CHARS - 1] + '…'

    try:
        paragraphs = core.justext_from_dom(dom, utils.get_stoplist("English"))
    except Exception as e:
        bad_bytes = sorted({b for b in content if b < 0x20 and b not in (0x09, 0x0A, 0x0D)})
        logger.exception("Error parsing paragraphs - offending control bytes: %s", bad_bytes)
        return {
            'url': url,
            'status': status_code,
            'timestamp': js_timestamp,
            'content': None,
            'error': {
                'name': e.__class__.__name__,
                'message': str(e),
            }
        }

    new_links, extra_links = get_new_links(paragraphs, url)

    # Also capture links from image-only anchors (e.g. icon links) that justext
    # drops because their paragraphs have no text nodes.
    for link in get_dom_links(dom, url):
        if link not in new_links and len(extra_links) < MAX_EXTRA_LINKS:
            extra_links.add(link)

    logger.debug(f"Got new links {new_links}")
    logger.debug(f"Got extra links {extra_links}")

    extract = ''
    for paragraph in paragraphs:
        if paragraph.class_type != 'good':
            continue
        extract += ' ' + paragraph.text.strip()
        if len(extract) > NUM_EXTRACT_CHARS:
            extract = extract[:NUM_EXTRACT_CHARS - 1] + '…'
            break

    # For JS-first pages (e.g. Discord, SPAs) justext finds no body content.
    # Fall back to Open Graph meta tags so these pages are still indexable.
    if not title or not extract:
        og_title, og_desc = get_og_meta(dom)
        if not title and og_title:
            title = og_title[:NUM_TITLE_CHARS - 1] + '…' if len(og_title) > NUM_TITLE_CHARS else og_title
        if not extract and og_desc:
            extract = og_desc[:NUM_EXTRACT_CHARS - 1] + '…' if len(og_desc) > NUM_EXTRACT_CHARS else og_desc

    return {
      'url': url,
      'status': status_code,
      'timestamp': js_timestamp,
      'content': {
        'title': title,
        'extract': extract,
        'links': sorted(new_links),
        'extra_links': sorted(extra_links),
      },
      'error': None
    }


def crawl_batch(batch, num_threads):
    with ThreadPool(num_threads) as pool:
        result = pool.map(crawl_url, batch)
    return result



