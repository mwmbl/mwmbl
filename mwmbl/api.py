"""
Unified Mwmbl API (v1).

All v1 endpoints are mounted here under a single NinjaExtraAPI instance so that
a single Swagger/OpenAPI docs page is available at /api/v1/docs.

Sub-routers:
  /api/v1/search/    — full-text search and autocomplete
  /api/v1/crawler/   — crawler batch submission and retrieval
  /api/v1/platform/  — user accounts, domain submissions, voting
  /api/v1/evaluate/  — WASM ranking function evaluation

JWT token endpoints (from NinjaJWTDefaultController) are also registered here,
typically at /api/v1/token/pair, /api/v1/token/refresh, etc.
"""

from ninja_extra import NinjaExtraAPI
from ninja_jwt.controller import NinjaJWTDefaultController

from mwmbl.exceptions import InvalidRequest
from scalar_ninja import ScalarViewer


api = NinjaExtraAPI(
    title="Mwmbl API",
    version="1.0.0",
    description=(
        "The Mwmbl open-source search engine API. "
        "Provides endpoints for searching the index, submitting crawl data, "
        "managing user accounts and domain submissions, and evaluating custom "
        "WASM ranking functions.\n\n"
        "## Authentication\n\n"
        "Most write endpoints require a JWT bearer token. "
        "Obtain a token pair by posting your credentials to `/api/v1/token/pair`. "
        "Include the access token in the `Authorization: Bearer <token>` header.\n\n"
        "Some crawler endpoints use a separate API key passed in the request body."
    ),
    urls_namespace="api-v1",
    docs=ScalarViewer(openapi_url="/api/v1/openapi.json"),
)

# Register JWT token endpoints (/token/pair, /token/refresh, /token/verify)
api.register_controllers(NinjaJWTDefaultController)


# Register the shared InvalidRequest exception handler
@api.exception_handler(InvalidRequest)
def invalid_request_handler(request, exc: InvalidRequest):
    return api.create_response(
        request,
        {"status": "error", "message": exc.message},
        status=exc.status,
    )


# Routers are imported after the api instance is created to avoid circular imports,
# and after init functions have been called from urls.py.
def register_routers(ranker, batch_cache, queued_batches):
    """
    Initialise and register all sub-routers on the unified API.

    This is called from urls.py after Django app setup is complete, so that
    dependencies (ranker, batch_cache, queued_batches) are available.
    """
    import mwmbl.tinysearchengine.search as search_module
    import mwmbl.crawler.app as crawler_module
    from mwmbl.platform.api import router as platform_router
    from mwmbl.evaluation.api import router as evaluation_router

    # Initialise routers that depend on runtime objects
    search_module.init_router(ranker)
    crawler_module.init_router(batch_cache, queued_batches)

    api.add_router("/search/", search_module.router, tags=["Search"])
    api.add_router("/crawler/", crawler_module.router, tags=["Crawler"])
    api.add_router("/platform/", platform_router, tags=["Platform"])
    api.add_router("/evaluate/", evaluation_router, tags=["Evaluation"])
