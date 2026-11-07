from logging import getLogger
from urllib.parse import urlparse

from mwmbl.indexer.blacklist_providers import BlacklistProvider

logger = getLogger(__name__)


def image_host_allowed(image_url: str, blacklist: BlacklistProvider) -> bool:
    """False if the image is hosted on a blacklisted domain, or its URL has no host to check.

    A clean page can hotlink from a blacklisted domain, so the page passing the blacklist says
    nothing about its images. The hostname is taken without any port or credentials, which
    would otherwise stop a blacklisted domain matching.
    """
    try:
        host = urlparse(image_url).hostname
    except ValueError:
        return False
    return host is not None and not blacklist.is_domain_blacklisted(host)


def drop_blacklisted_images(results: list[dict], blacklist: BlacklistProvider) -> int:
    """Remove images on blacklisted hosts from crawl results, in place. Returns how many went.

    Done in the crawler because it costs a volunteer nothing to leave them out and saves
    sending them. The server checks again, as a crawler is not trusted to have.
    """
    dropped = 0
    for result in results:
        content = result["content"]
        if content is None:
            continue
        images = content["images"]
        content["images"] = [image for image in images if image_host_allowed(image["url"], blacklist)]
        dropped += len(images) - len(content["images"])
    if dropped:
        logger.info(f"Dropped {dropped} images hosted on blacklisted domains")
    return dropped
