import glob
import gzip
import json

import django
from django.conf import settings

django.setup()


from mwmbl.crawler.batch import HashedBatch

CRAWL_GLOB = "./devdata/batches/**/*.json.gz"




def get_urls():
    for path in glob.glob(CRAWL_GLOB, recursive=True):
        data = json.load(gzip.open(path))
        batch = HashedBatch.parse_obj(data)
        user = batch.user_id_hash
        for item in batch.items:
            if item.content is not None:
                for url in item.content.links:
                    yield user, url


def run():
    for user, url in get_urls():
        print(user, url)



if __name__ == "__main__":
    run()
