"""
Authentication for the search API using the X-API-Key header.
"""
import hashlib

from asgiref.sync import sync_to_async
from django.core.cache import cache
from ninja.errors import HttpError
from ninja.security import APIKeyHeader

from mwmbl.models import ApiKey, MwmblUser

CACHE_TTL = 3600  # 1 hour; explicit invalidation handles all mutation paths


def _cache_key(key_hash: str) -> str:
    return f"search:apikey:{key_hash}"


def invalidate_api_key_cache(key_hash: str) -> None:
    cache.delete(_cache_key(key_hash))


def invalidate_user_api_key_cache(user_id: int) -> None:
    key_hashes = ApiKey.objects.filter(
        user_id=user_id,
        scopes__contains=[ApiKey.Scope.SEARCH],
    ).values_list("key", flat=True)
    cache.delete_many([_cache_key(h) for h in key_hashes])


class SearchApiKeyAuth(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request, key: str | None):
        if not key:
            return None
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        cache_key = _cache_key(key_hash)

        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            api_key = ApiKey.objects.select_related("user").get(
                key=key_hash,
                scopes__contains=[ApiKey.Scope.SEARCH],
            )
        except ApiKey.DoesNotExist:
            return None

        cache.set(cache_key, api_key, CACHE_TTL)
        return api_key


async def authenticate_user(request) -> MwmblUser:
    """Resolve the requesting user from either an X-API-Key header or a JWT.

    Returns the user on success; raises ``HttpError(401)`` otherwise. Database
    lookups are off-loaded via ``sync_to_async`` so this can be awaited from
    async views.
    """
    raw_key = request.headers.get("X-API-Key")
    if raw_key:
        api_key = await sync_to_async(SearchApiKeyAuth().authenticate)(request, raw_key)
        if api_key is None:
            raise HttpError(401, "Invalid API key.")
        return await sync_to_async(lambda: api_key.user)()

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        # Defer JWT validation and user lookup to ninja-jwt rather than decoding
        # the token by hand: this respects the configured token classes, user-id
        # claim/field, and the is_active check.
        from ninja_jwt.authentication import AsyncJWTAuth
        from ninja_jwt.exceptions import AuthenticationFailed, TokenError

        token = auth_header[len("Bearer "):]
        try:
            return await AsyncJWTAuth().async_jwt_authenticate(request, token)
        except (TokenError, AuthenticationFailed):
            raise HttpError(401, "Invalid token.")

    raise HttpError(401, "Authentication required: X-API-Key or Bearer token.")
