"""The pages for Mwmbl's hosted Mwmbl Social instance: server sign-up and its privacy policy.

social.mwmbl.org's provider_info points fediverse admins here: its sign_in_url and
root_redirect at /social/connect/, and its privacy_policy_url at /social/privacy/.
"""

from django.conf import settings
from django.shortcuts import render


def privacy(request):
    return render(request, "social/privacy.html", {"footer_links": settings.FOOTER_LINKS})
