"""The licences a Commons file can be indexed under, as the DocumentSource members that carry them.

Commons names a file's licence in free text (`LicenseShortName`), with 62 distinct values over
17,607 Seed Search candidates, but the ten below cover 94% of them. The rest share
COMMONS_OTHER, and the result page then points at the file page, which names the licence.
"""

import re

from mwmbl.tinysearchengine.indexer import DocumentSource

CC_URL = "https://creativecommons.org"

# Short names as Commons writes them, with the licence's name and page. Matched exactly, so a
# regional variant such as `CC BY-SA 3.0 de` is COMMONS_OTHER rather than a guess.
LICENCES = {
    "Public domain": (DocumentSource.COMMONS_PUBLIC_DOMAIN, "Public domain", f"{CC_URL}/publicdomain/mark/1.0/"),
    "CC0": (DocumentSource.COMMONS_CC0, "CC0", f"{CC_URL}/publicdomain/zero/1.0/"),
    "CC BY 2.0": (DocumentSource.COMMONS_CC_BY_2_0, "CC BY 2.0", f"{CC_URL}/licenses/by/2.0/"),
    "CC BY 2.5": (DocumentSource.COMMONS_CC_BY_2_5, "CC BY 2.5", f"{CC_URL}/licenses/by/2.5/"),
    "CC BY 3.0": (DocumentSource.COMMONS_CC_BY_3_0, "CC BY 3.0", f"{CC_URL}/licenses/by/3.0/"),
    "CC BY 4.0": (DocumentSource.COMMONS_CC_BY_4_0, "CC BY 4.0", f"{CC_URL}/licenses/by/4.0/"),
    "CC BY-SA 2.0": (DocumentSource.COMMONS_CC_BY_SA_2_0, "CC BY-SA 2.0", f"{CC_URL}/licenses/by-sa/2.0/"),
    "CC BY-SA 2.5": (DocumentSource.COMMONS_CC_BY_SA_2_5, "CC BY-SA 2.5", f"{CC_URL}/licenses/by-sa/2.5/"),
    "CC BY-SA 3.0": (DocumentSource.COMMONS_CC_BY_SA_3_0, "CC BY-SA 3.0", f"{CC_URL}/licenses/by-sa/3.0/"),
    "CC BY-SA 4.0": (DocumentSource.COMMONS_CC_BY_SA_4_0, "CC BY-SA 4.0", f"{CC_URL}/licenses/by-sa/4.0/"),
}
OTHER_LICENCE_NAME = "Other licence"
LICENCE_NAMES_AND_URLS = {source: (name, url) for source, name, url in LICENCES.values()}
LICENCE_NAMES_AND_URLS[DocumentSource.COMMONS_OTHER] = (OTHER_LICENCE_NAME, "")
WHITESPACE_PATTERN = re.compile(r"\s+")


def licence_source(license_name: str) -> DocumentSource:
    """The DocumentSource for a Commons `LicenseShortName`; COMMONS_OTHER for any we don't list."""
    normalised = WHITESPACE_PATTERN.sub(" ", license_name).strip()
    if normalised not in LICENCES:
        return DocumentSource.COMMONS_OTHER
    source, _, _ = LICENCES[normalised]
    return source
