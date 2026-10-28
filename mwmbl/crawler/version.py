# No imports: the crawler release workflow reads this without installing the project, and
# refuses to publish a crawler-vX.Y.Z tag that does not match it.
CRAWLER_VERSION: str = "0.2.0"
