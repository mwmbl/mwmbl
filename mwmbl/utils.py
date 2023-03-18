import re

DOMAIN_REGEX = re.compile(r".*://([^/]*)")


def batch(items: list, batch_size):
    """
    Adapted from https://stackoverflow.com/a/8290508
    """
    length = len(items)
    for ndx in range(0, length, batch_size):
        yield items[ndx:min(ndx + batch_size, length)]


def get_domain(url):
    results = DOMAIN_REGEX.match(url)
    if results is None or len(results.groups()) == 0:
        raise ValueError(f"Unable to parse domain from URL {url}")
    return results.group(1)
