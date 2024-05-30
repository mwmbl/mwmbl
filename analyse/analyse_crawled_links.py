import glob
import gzip
import json
from collections import Counter
from urllib.parse import urlparse

import django
from django.conf import settings

django.setup()


from mwmbl.crawler.batch import HashedBatch

CRAWL_GLOB = "./devdata/batches/**/2024-05-25/**/*.json.gz"




def get_urls():
    for path in glob.glob(CRAWL_GLOB, recursive=True):
        print("Getting data from path", path)
        data = json.load(gzip.open(path))
        batch = HashedBatch.parse_obj(data)
        user = batch.user_id_hash
        for item in batch.items:
            if item.content is not None:
                yield item.url, item.content.links, item.content.extra_links


def run():
    link_counts = Counter()
    source_counts = Counter()
    extra_counts = Counter()
    for source, urls, extra_urls in get_urls():
        source_domain = urlparse(source).netloc
        source_counts.update([source_domain])

        for url in urls:
            link_domain = urlparse(url).netloc
            link_counts.update([link_domain])

        for url in extra_urls:
            extra_domain = urlparse(url).netloc
            extra_counts.update([extra_domain])

        if len(link_counts) > 1000:
            break

    print("Source counts", source_counts.most_common())
    print("Link counts", link_counts.most_common())
    print("Extra counts", extra_counts.most_common())

if __name__ == "__main__":
    run()
