"""
Authentication for the search API using the X-API-Key header.
"""
from ninja.security import APIKeyHeader

from mwmbl.models import ApiKey


class SearchApiKeyAuth(APIKeyHeader):
    """
    Ninja authentication class that reads the X-API-Key request header and
    validates it against ApiKey rows that have the 'search' scope.

    On success, request.auth is set to the ApiKey instance (giving access to
    request.auth.user and request.auth.user.tier).
    On failure, returns None which causes Ninja to respond with 401.
    """

    param_name = "X-API-Key"

    def authenticate(self, request, key: str):
        try:
            return ApiKey.objects.select_related("user").get(
                key=key,
                scopes__contains=[ApiKey.Scope.SEARCH],
            )
        except ApiKey.DoesNotExist:
            return None
