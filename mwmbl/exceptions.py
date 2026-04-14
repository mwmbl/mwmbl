"""
Shared exception classes for the Mwmbl API.
"""


class InvalidRequest(Exception):
    """Raised when a request is invalid or the user lacks permission.

    The default status is 400 (Bad Request). Use status=403 explicitly for
    permission/authorisation errors.
    """

    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)
