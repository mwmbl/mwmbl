import os

import django
import uvicorn
from django.core.management import call_command
from redis import Redis

from mwmbl.indexer.update_urls import update_urls_continuously
from mwmbl.redis_url_queue import RedisURLQueue


def run():
    django.setup()

    from django.conf import settings
    from mwmbl import background

    if settings.STATIC_ROOT:
        call_command("collectstatic", "--clear", "--noinput")

    call_command("migrate")

    mwmbl_app = os.environ["MWMBL_APP"]
    if mwmbl_app == "update_urls":
        redis: Redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379"), decode_responses=True)
        url_queue = RedisURLQueue(redis)
        update_urls_continuously(settings.DATA_PATH, url_queue)
    elif mwmbl_app == "update_batches":
        background.run(settings.DATA_PATH)
    elif mwmbl_app == "copy_indexes":
        background.copy_indexes_continuously()
    elif mwmbl_app == "server":
        uvicorn.run("mwmbl.asgi:application", host="0.0.0.0", port=5000)
    else:
        raise ValueError(f"Unknown MWMBL_APP: {mwmbl_app}")


if __name__ == "__main__":
    run()
