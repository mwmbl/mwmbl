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

from mwmbl.justext.core import html_to_dom
from mwmbl.justext.paragraph import Paragraph

ALLOWED_EXCEPTIONS = (ValueError, ConnectionError, ReadTimeout, TimeoutError,
                      OSError, NewConnectionError, MaxRetryError, SSLCertVerificationError)

POST_BATCH_URL = '/api/v1/crawler/batches/'
POST_NEW_BATCH_URL = '/api/v1/crawler/batches/new'

TIMEOUT_SECONDS = 3
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


logger = getLogger(__name__)


def fetch(url):
    """
    Fetch with a maximum timeout and maximum fetch size to avoid big pages bringing us down.

    https://stackoverflow.com/a/22347526
    """

    r = requests.get(url, stream=True, timeout=TIMEOUT_SECONDS)

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


def robots_allowed(url):
    try:
        parsed_url = urlparse(url)
    except ValueError:
        logger.info(f"Unable to parse URL: {url}")
        return False

    if parsed_url.path.rstrip('/') == '' and parsed_url.query == '':
        logger.debug(f"Allowing root domain for URL: {url}")
        return True

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
    allowed = parse_robots.can_fetch('Mwmbl', url)
    logger.debug(f"Robots allowed for {url}: {allowed}")
    return allowed


def get_new_links(paragraphs: list[Paragraph], current_url):
    new_links = set()
    extra_links = set()
    parsed_url = urlparse(current_url)
    base_url = urlunsplit((parsed_url.scheme, parsed_url.netloc, "", "", ""))

    for paragraph in paragraphs:
        if len(paragraph.links) > 0:
            for link in paragraph.links:
                if not link.startswith("http"):
                    if "://" in link:
                        logger.debug(f"Bad URL: {link}")
                        continue

                    # Relative link
                    if link.startswith("/"):
                        link = urljoin(base_url, link)
                    else:
                        link = urljoin(current_url, link)

                if link.startswith('http') and len(link) <= MAX_URL_LENGTH:
                    if BAD_URL_REGEX.search(link):
                        logger.debug(f"Found bad URL: {link}")
                        continue
                    try:
                        parsed_url = urlparse(link)
                    except ValueError:
                        logger.info(f"Unable to parse link {link}")
                        continue
                    url_without_hash = urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path, parsed_url.query, ''))
                    if paragraph.class_type == 'good':
                        if len(new_links) < MAX_NEW_LINKS:
                            new_links.add(url_without_hash)
                    else:
                        if len(extra_links) < MAX_EXTRA_LINKS and url_without_hash not in new_links:
                            extra_links.add(url_without_hash)
                if len(new_links) >= MAX_NEW_LINKS and len(extra_links) >= MAX_EXTRA_LINKS:
                    return new_links, extra_links
    return new_links, extra_links


def crawl_url(url):
    logger.info(f"Crawling URL {url}")
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
        logger.exception("Error parsing paragraphs")
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



