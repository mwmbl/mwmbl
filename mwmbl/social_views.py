"""The pages for Mwmbl's hosted Mwmbl Social instance: server sign-up, its privacy policy, and
search over the posts it has indexed.

social.mwmbl.org's provider_info points fediverse admins here: its sign_in_url and
root_redirect at /social/connect/, and its privacy_policy_url at /social/privacy/.
"""

from dataclasses import dataclass, field
from logging import getLogger

import httpx
from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from mwmbl import social
from mwmbl.models import SocialRegistration

logger = getLogger(__name__)

REFUSAL_MESSAGES = {
    400: "Mwmbl Social can't use that address. Enter your server's https address, for example "
    "https://mastodon.example.",
    409: "That server is already connected to Mwmbl Social, or waiting for its admin to approve the "
    "connection. Its admin can find it under Administration > Fediverse Auxiliary Service Providers.",
    502: "Mwmbl Social couldn't register with that server: it couldn't be reached, isn't FASP-enabled, "
    "or refused. Mastodon needs version 4.5 or later with EXPERIMENTAL_FEATURES=fasp set. If the server "
    "was connected before, its admin must first delete the old Mwmbl Social provider: Mastodon refuses a "
    "second registration until then.",
}
STATUS_LABELS = {
    "pending": "Waiting for the server admin's approval",
    "active": "Connected",
    "revoked": "Disconnected",
}
_https_only = URLValidator(schemes=["https"])


class ConnectRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ServerForm(forms.Form):
    server_url = forms.CharField(label="Your server's address", max_length=200)

    def clean_server_url(self) -> tuple[str, str]:
        try:
            return social.server_url_and_host(self.cleaned_data["server_url"].strip())
        except ValueError as error:
            raise ValidationError(str(error)) from error


@require_http_methods(["GET", "POST"])
def connect(request):
    if not request.user.is_authenticated:
        if request.method == "POST":
            return redirect_to_login(request.path)
        return _render_connect(request, form=None)

    form = ServerForm(request.POST or None)
    if not form.is_valid():
        return _render_connect(request, form, status=400 if form.is_bound else 200)

    server_url, host = form.cleaned_data["server_url"]
    try:
        registration = _register(request.user, server_url, host)
    except ConnectRefused as refusal:
        return _render_connect(request, form, error=refusal.message, status=refusal.status)
    return redirect(f"{reverse('social_connect')}#registration-{registration.pk}")


def _register(user, server_url: str, host: str) -> SocialRegistration:
    if not social.take_user_attempt(user.id):
        raise ConnectRefused(
            429,
            f"You can try to connect {settings.SOCIAL_CONNECT_ATTEMPTS_PER_USER_PER_DAY} servers a day. Try again tomorrow.",
        )
    if not social.take_host_attempt(host):
        raise ConnectRefused(
            429, "Someone tried to connect that server in the last hour. Check its admin panel, or try again later."
        )
    try:
        created = social.register(server_url)
    except social.RegistrationRefused as refused:
        raise ConnectRefused(refused.status, REFUSAL_MESSAGES[refused.status]) from refused
    except httpx.TransportError as error:
        # The registration may still have gone through, leaving a pending provider whose
        # approval link we never saw.
        logger.exception("Mwmbl Social did not answer a registration of %s", server_url)
        raise ConnectRefused(
            503,
            "Mwmbl Social didn't answer in time. If your server's admin panel now lists Mwmbl Social, "
            "delete it there before trying again.",
        ) from error

    approval_url = created["approval_url"]
    try:
        _https_only(approval_url)
    except ValidationError:
        approval_url = ""
    return SocialRegistration.objects.create(
        user=user,
        server_url=server_url,
        registration_id=created["id"],
        base_url=created["base_url"],
        fingerprint=created["fingerprint"],
        approval_url=approval_url,
    )


def _render_connect(request, form, error: str | None = None, status: int = 200):
    rows = []
    if request.user.is_authenticated:
        rows = _rows_with_live_status(list(request.user.social_registrations.order_by("-created_at")))
    context = {
        "form": form,
        "error": error,
        "rows": rows,
        "capabilities": ["data_sharing", "account_search", "follow_recommendation"],
        "footer_links": settings.FOOTER_LINKS,
    }
    return render(request, "social/connect.html", context, status=status)


@dataclass
class RegistrationRow:
    registration: SocialRegistration
    status: str
    capabilities: list[str] = field(default_factory=list)


def _rows_with_live_status(registrations: list[SocialRegistration]) -> list[RegistrationRow]:
    """Each registration with Mwmbl Social's view of it, saying so when that is unavailable."""
    if all(registration.disconnected_at for registration in registrations):
        live = {}
    else:
        try:
            live = social.registrations_by_id()
        except httpx.HTTPError:
            logger.exception("Could not read registration statuses from Mwmbl Social")
            live = None
    return [_row(registration, live) for registration in registrations]


def _row(registration: SocialRegistration, live: dict[int, dict] | None) -> RegistrationRow:
    if registration.disconnected_at:
        return RegistrationRow(registration, "Disconnected")
    if live is None:
        return RegistrationRow(registration, "Status unavailable: Mwmbl Social didn't answer")
    if registration.registration_id not in live:
        # Replaced by a newer registration of the same server.
        return RegistrationRow(registration, "No longer registered with Mwmbl Social")
    current = live[registration.registration_id]
    return RegistrationRow(registration, STATUS_LABELS[current["status"]], current["capabilities"])


@login_required
@require_POST
def disconnect(request, pk: int):
    registration = get_object_or_404(SocialRegistration, pk=pk, user=request.user, disconnected_at__isnull=True)
    social.revoke(registration.registration_id)
    registration.disconnected_at = timezone.now()
    registration.save(update_fields=["disconnected_at"])
    return redirect(f"{reverse('social_connect')}#registration-{registration.pk}")


def privacy(request):
    return render(request, "social/privacy.html", {"footer_links": settings.FOOTER_LINKS})


def search(request):
    query = request.GET.get("q", "").strip()
    posts = []
    unavailable = False
    if query:
        try:
            posts = social.search_posts(query)
        except httpx.HTTPError as error:
            # Not the exception itself: its message carries the URL, and with it the query.
            logger.warning("Mwmbl Social post search failed with %s", type(error).__name__)
            unavailable = True
    context = {
        "query": query,
        "posts": posts,
        "unavailable": unavailable,
        "footer_links": settings.FOOTER_LINKS,
    }
    response = render(request, "social/search.html", context, status=503 if unavailable else 200)
    # Other search engines indexing this page would copy the posts beyond the reach of Mwmbl
    # Social's removals.
    response["X-Robots-Tag"] = "noindex"
    return response
