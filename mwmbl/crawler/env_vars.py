"""
Environment variables for the server's own fetches of other sites (mwmbl/crawler/retrieve.py)
and for the clients that read the main index. The crawler volunteers run is configured by
its own command line, see rust/crawler.
"""

import os

# Delay in seconds between the fetches a moderation evidence crawl makes.
CRAWL_DELAY_SECONDS = float(os.environ.get("CRAWL_DELAY_SECONDS", "0.0"))

# Contact information for site administrators, sent in the User-Agent of every fetch.
MWMBL_CONTACT_INFO = os.environ.get("MWMBL_CONTACT_INFO", "CHANGE_ME@example.com")

# Where RemoteIndex reads the main index from. Every rankeval script that constructs
# RemoteIndex() with no argument gets this, so an evaluation sweep is not aimed at
# api.mwmbl.org by default. Beta exists precisely so that it does not have to.
MWMBL_REMOTE_SERVER = os.environ.get("MWMBL_REMOTE_SERVER", "https://api.mwmbl.org")
