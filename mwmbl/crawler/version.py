# No imports: the crawler release workflow reads this without installing the project. Raising
# it on main publishes a release; CI fails a pull request that changes the crawler without
# raising it (mwmbl/crawler/release_check.py).
CRAWLER_VERSION: str = "1.0.1"
