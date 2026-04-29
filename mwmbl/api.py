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
typically at /api/v1/platform/token/pair, /api/v1/platform/token/refresh, etc.
"""

from ninja import Field
from ninja_extra import NinjaExtraAPI, api_controller, http_post
from ninja_extra.permissions import AllowAny
from ninja_jwt.controller import TokenVerificationController, TokenObtainPairController
from ninja_jwt.schema import TokenObtainPairInputSchema
from ninja_jwt.schema_control import SchemaControl
from ninja_jwt.settings import api_settings

from mwmbl.exceptions import InvalidRequest
from scalar_ninja import ScalarViewer
from scalar_ninja.scalar_ninja import AgentConfig

_schema = SchemaControl(api_settings)


class MwmblTokenObtainSchema(TokenObtainPairInputSchema):
    username: str = Field(description="Your username or email address.")
    password: str = Field(description="Your password.")


@api_controller("/platform/token", permissions=[AllowAny], tags=["Authentication"], auth=None)
class MwmblTokenController(TokenVerificationController, TokenObtainPairController):
    """JWT token endpoints for authentication."""

    auto_import = False

    @http_post(
        "/pair",
        response=MwmblTokenObtainSchema.get_response_schema(),
        url_name="token_obtain_pair",
        operation_id="token_obtain_pair",
        summary="Obtain JWT token pair",
        description=(
            "Exchange credentials for a JWT access/refresh token pair. "
            "The `username` field accepts either a **username** or an **email address**. "
            "Include the returned `access` token in the `Authorization: Bearer <token>` "
            "header on subsequent requests."
        ),
    )
    def obtain_token(self, user_token: MwmblTokenObtainSchema):
        user_token.check_user_authentication_rule()
        return user_token.to_response_schema()

api = NinjaExtraAPI(
    title="Mwmbl API",
    version="1.0.0",
    description=(
        "The Mwmbl open-source search engine API. "
        "Provides endpoints for searching the index, submitting crawl data, "
        "and managing user accounts and domain submissions.\n\n"
        "## Authentication\n\n"
        "Most write endpoints require a JWT bearer token. "
        "Obtain a token pair by posting your credentials to `/api/v1/platform/token/pair`. "
        "You can log in with either your **username** or your **email address**. "
        "Include the access token in the `Authorization: Bearer <token>` header.\n\n"
        "Some crawler endpoints use a separate API key passed in the request body."
    ),
    urls_namespace="api-v1",
    docs=ScalarViewer(openapi_url="/api/v1/openapi.json", agent=AgentConfig(disabled=True)),
    openapi_extra={
        "tags": [
            {"name": "Search"},
            {"name": "Crawler"},
            {"name": "Authentication"},
            {"name": "Platform"},
        ]
    },
)

# Register JWT token endpoints (/platform/token/pair, /platform/token/refresh, /platform/token/verify)
api.register_controllers(MwmblTokenController)


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

    # This API is a work in progress, disabled for now
    # api.add_router("/evaluate/", evaluation_router, tags=["Evaluation"])
