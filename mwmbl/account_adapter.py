from urllib.parse import urlencode

from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings


class MwmblAccountAdapter(DefaultAccountAdapter):
    def get_email_confirmation_url(self, request, emailconfirmation):
        # allauth builds the link from the request's host, which is the API's, but people
        # confirm on the front end, which posts the key and email to /platform/confirm-email.
        query = urlencode({"key": emailconfirmation.key, "email": emailconfirmation.email_address.email})
        return f"{settings.FRONTEND_URL}/account/confirm-email?{query}"
