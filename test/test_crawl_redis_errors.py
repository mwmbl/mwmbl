"""What a Redis that answers with an error costs the crawler.

A Redis whose background save fails rejects every write with "MISCONF ... unable to
persist to disk". check_redis converted a connection failure and let everything else
through, and both continuous loops called it outside their try, so the error killed the
process. run() exits the whole crawler once the indexing process has died six times in an
hour, which is how one full disk on the Redis host stopped a crawler crawling.
"""

from unittest.mock import MagicMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from mwmbl.crawl import Crawler

MISCONF = ResponseError(
    "MISCONF Redis is configured to save RDB snapshots, but it is currently unable to persist to disk."
)


class LoopStopped(BaseException):
    """Breaks the while True once it has done what the test is watching for.

    A BaseException so the loop's own `except Exception` cannot swallow it.
    """


def crawler_with_failing_redis(error):
    crawler = Crawler()
    crawler._redis = MagicMock()
    crawler._redis.ping.side_effect = error
    return crawler


@pytest.mark.parametrize(
    "loop_name, work",
    [("process_batch_continuously", "process_batch"), ("run_indexing_continuously", "run_indexing")],
)
def test_a_redis_error_is_waited_out_not_died_of(loop_name, work):
    crawler = crawler_with_failing_redis(MISCONF)

    with (
        patch.object(Crawler, work) as do_work,
        patch("mwmbl.crawl.time.sleep", side_effect=LoopStopped) as sleep,
    ):
        with pytest.raises(LoopStopped):
            getattr(crawler, loop_name)()

    # Reaching the sleep is the point: the loop caught the error and will try again.
    sleep.assert_called_once_with(10)
    do_work.assert_not_called()


@pytest.mark.parametrize("loop_name", ["process_batch_continuously", "run_indexing_continuously"])
def test_a_redis_that_cannot_be_reached_still_ends_the_process(loop_name):
    """Retrying is for a Redis that answers. Nothing here can fix one that is not there."""
    crawler = crawler_with_failing_redis(RedisConnectionError("Connection refused"))

    with patch("mwmbl.crawl.time.sleep", side_effect=LoopStopped):
        with pytest.raises(SystemExit):
            getattr(crawler, loop_name)()
