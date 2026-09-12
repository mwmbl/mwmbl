"""Count every search request as it leaves.

Sync-only, and measured rather than assumed: the app runs under uvicorn workers, but
allauth's AccountMiddleware is sync-only and Django adapts everything above a sync
middleware to sync too, so an async branch here would never execute.
test_the_chain_runs_sync_because_of_allauth pins that.

Two things follow. The sync chain already runs off the event loop, on the thread asgiref
hands it, so a blocking Redis call here cannot block the loop. And it is one thread for the
whole chain, so the work stays a single pipeline with short socket timeouts.
"""

from mwmbl.traffic import record_request


class SearchTrafficMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # After the view, so resolver_match is populated. For the Super Search SSE view this
        # is the start of the stream rather than the end of it, which is the right moment to
        # count a search - but it means the response has no body to look at yet, and reading
        # response.content would raise. Nothing here touches it.
        record_request(request)
        return response
