"""
Authentication for the search API using the X-API-Key header.
"""
import hashlib

from ninja.security import APIKeyHeader

from mwmbl.models import ApiKey


class SearchApiKeyAuth(APIKeyHeader):
    param_name = "X-API-Key"

    def authenticate(self, request, key: str):
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        try:
            return ApiKey.objects.select_related("user").get(
                key=key_hash,
                scopes__contains=[ApiKey.Scope.SEARCH],
            )
        except ApiKey.DoesNotExist:
            return None
