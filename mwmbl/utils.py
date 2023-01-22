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
    domain = DOMAIN_REGEX.search(url)[0]
    return domain
