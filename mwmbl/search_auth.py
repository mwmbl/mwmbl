"""
Authentication for the search API using the X-API-Key header.
"""
import hashlib

from django.core.cache import cache
from ninja.security import APIKeyHeader

from mwmbl.models import ApiKey

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

    def authenticate(self, request, key: str):
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
