from datetime import datetime
from typing import Optional, Literal
from ninja import Schema, ModelSchema, Field
from pydantic import model_validator

from mwmbl.models import AgreementType, DomainSubmission, MarketingSource


class UserProfileResponse(Schema):
    username: str
    email: str
    plan: str
    email_confirmed: bool


class SubscriptionResponse(Schema):
    status: str
    max_monthly_spend_cents: int
    monthly_limit: int
    monthly_usage: int
    estimated_cost_cents: int
    current_period_end: Optional[datetime]
    polar_customer_id: Optional[str]


class CheckoutRequest(Schema):
    success_url: Optional[str] = None
    embed_origin: Optional[str] = None


class CheckoutResponse(Schema):
    checkout_url: str


class UpdateSpendLimitRequest(Schema):
    max_monthly_spend_cents: int = Field(ge=0)


class ForgotPasswordRequest(Schema):
    email: str


class ResetPasswordRequest(Schema):
    email: str
    key: str
    new_password: str


# ---------------------------------------------------------------------------
# API key management schemas
# ---------------------------------------------------------------------------

class CreateApiKeyRequest(Schema):
    """Request body for creating a new API key."""
    name: str = Field(
        default="",
        max_length=100,
        description="Optional human-readable label for this key.",
        example="My search app",
    )
    scope: Literal["search", "crawl"] = Field(
        default="search",
        description="Scope for this key. Use 'crawl' for the crawler endpoint, 'search' for the search endpoint.",
    )


class ApiKeyCreatedResponse(Schema):
    """
    Response returned when a new API key is created.
    The raw `key` value is only returned once — store it securely.
    """
    id: int = Field(description="Unique ID of the API key.")
    key: str = Field(description="The raw API key token. Shown only on creation.")
    name: str = Field(description="Human-readable label for this key.")
    created_on: datetime = Field(description="When the key was created.")
    scopes: list[str] = Field(description="Scopes granted to this key.")


class ApiKeyListItem(Schema):
    """
    A single API key entry returned by the list endpoint.
    The raw key value is intentionally omitted.
    """
    id: int = Field(description="Unique ID of the API key.")
    name: str = Field(description="Human-readable label for this key.")
    created_on: datetime = Field(description="When the key was created.")
    scopes: list[str] = Field(description="Scopes granted to this key.")


# ---------------------------------------------------------------------------
# Agreements
# ---------------------------------------------------------------------------

class AgreementAcceptRequest(Schema):
    agreement_type: AgreementType = Field(description="The type of agreement being accepted.")


class AgreementResponse(Schema):
    agreement_type: str = Field(description="The type of agreement.")
    version_id: str = Field(description="The version of the agreement that was accepted.")
    accepted_at: datetime = Field(description="When the agreement was accepted.")


# ---------------------------------------------------------------------------
# Marketing consent
# ---------------------------------------------------------------------------

class MarketingConsentRequest(Schema):
    source: MarketingSource = Field(description="The property the consent applies to (`GUI` or `API`).")
    opted_in: bool = Field(description="True to opt in to marketing emails, False to withdraw consent.")


class MarketingConsentResponse(Schema):
    source: str = Field(description="The property the consent applies to.")
    opted_in: bool = Field(description="The current opt-in state for this source.")
    timestamp: datetime = Field(description="When this consent decision was recorded.")


class MarketingConsentListResponse(Schema):
    consent: list[MarketingConsentResponse] = Field(
        description="Current marketing consent state per source the user has a record for.",
    )


class Registration(Schema):
    email: str = Field(description="Email address for the new account. Must be unique.")
    password: str = Field(description="Password for the new account.")
    username: Optional[str] = Field(
        default=None,
        description=(
            "Optional username. If omitted, one is generated automatically in the form "
            "`adjective_noun_NNN` (e.g. `swift_falcon_379`). Must be unique if provided."
        ),
    )
    agreements: list[AgreementType] = Field(
        default=[],
        description=(
            "Optional list of agreement types accepted at signup. "
            "The server stamps the current version and timestamp. "
            "Accepted values: `TERMS_OF_SERVICE_GUI`, `TERMS_OF_SERVICE_API`."
        ),
    )
    source: Optional[MarketingSource] = Field(
        default=None,
        description=(
            "Which Mwmbl property the user signed up from (`GUI` = mwmbl.org, "
            "`API` = developer.mwmbl.org). Determines the type of marketing email. "
            "When provided, the marketing opt-in decision is recorded against this source."
        ),
    )
    marketing_opt_in: bool = Field(
        default=False,
        description=(
            "Whether the user opted in to marketing emails. Must reflect an affirmative, "
            "unticked-by-default checkbox. Only recorded when `source` is also provided."
        ),
    )


class ConfirmEmail(Schema):
    email: str = Field(description="Email address to confirm.")
    key: str = Field(description="Confirmation key from the verification email.")
    username: Optional[str] = Field(
        default=None,
        description="Deprecated — ignored. Accepted for backwards compatibility only.",
    )


class RejectionFieldsMixin:
    """Validate the rejection reason and its detail, wherever a decision arrives.

    Django enforces neither of these. ``choices`` is not checked on ``save()``, so an
    arbitrary string of twenty characters or fewer lands in the column and comes back out at
    a moderator as a reason nothing knows how to render; and nothing has ever required the
    detail that "Other - needs detail" promises, which is the only place a rejected submitter
    finds out what was actually wrong.

    A schema validator rather than a check in the view, so these come back as a 422 naming
    the field, the same as an over-long audit value.
    """

    @model_validator(mode="after")
    def _check_rejection(self):
        # Both columns are blank=True, so ninja types them Optional and an omitted field
        # arrives as None rather than "".
        reason = self.rejection_reason or ""
        detail = self.rejection_detail or ""
        if reason and reason not in DomainSubmission.DOMAIN_REJECTION_REASON:
            allowed = ", ".join(DomainSubmission.DOMAIN_REJECTION_REASON)
            raise ValueError(f"rejection_reason must be one of: {allowed}")
        if reason and self.status != "REJECTED":
            raise ValueError("rejection_reason is only meaningful when status is REJECTED")
        if reason == "OTHER" and not detail.strip():
            raise ValueError(
                "rejection_detail is required when rejection_reason is OTHER - it is what "
                "the submitter is shown")
        return self


class DomainSubmissionSchema(ModelSchema):
    class Meta:
        model = DomainSubmission
        fields = ["id", "name", "submitted_by", "submitted_on", "status", "rejection_reason", "rejection_detail"]


class UpdateDomainSubmission(RejectionFieldsMixin, ModelSchema):
    class Meta:
        model = DomainSubmission
        fields = ["status", "rejection_reason", "rejection_detail"]

    # Echoed back by the client so we record what the moderator was actually looking at.
    # Optional: a client that shows no suggestions simply omits them, and a decision made
    # without a suggestion is stored as exactly that rather than as a suggestion of "".
    #
    # The lengths match the columns these land in. Django does not enforce max_length on
    # save(), so without them an over-long value is a 500 from Postgres where it should be a
    # 400 naming the field.
    suggested_status: Optional[str] = Field(
        default=None, max_length=20,
        description="The action the tool suggested, as shown to the moderator.")
    suggested_reason: Optional[str] = Field(
        default=None, max_length=20,
        description="The rejection reason the tool suggested, if any.")
    suggestion_confidence: Optional[float] = Field(
        default=None, description="Confidence of the suggestion that was shown.")
    suggestion_model_version: Optional[str] = Field(
        default=None, max_length=50,
        description="Version of the model that produced the shown suggestion.")


class EvidenceItemSchema(Schema):
    """One checkable fact behind a suggestion."""
    kind: str = Field(description="Machine-readable check name, e.g. `http_status`.")
    direction: str = Field(description="`reject`, `approve` or `neutral`.")
    label: str = Field(description="Moderator-facing text, e.g. 'Homepage returns HTTP 404'.")


class SuggestionSchema(Schema):
    action: str = Field(description="`APPROVE`, `REJECT` or `UNSURE`.")
    confidence: float
    reason: str = Field(default="", description="Rejection reason; empty unless action is REJECT.")
    reason_confidence: float = 0.0
    reason_source: str = Field(
        default="model",
        description=(
            "`rule` when a deterministic check decided it, `model` when the classifier did, "
            "`derived` when the reason class is learned from public blocklists rather than "
            "from moderator decisions and should be treated as a weaker hint."
        ),
    )
    model_version: str = ""
    evidence: list[EvidenceItemSchema] = []


class CrawledPageSchema(Schema):
    url: str
    status: Optional[int] = None
    title: str = ""
    extract: str = ""
    num_links: int = 0
    error: str = ""


class QueueItemSchema(Schema):
    """One pending domain: everything the review card and its up-next row draw.

    Keyed on the domain, not on a submission. A domain submitted nine times is one thing to
    look at and one decision to make, so there is no single submission id behind a row -
    decisions are addressed to `name` (see `DomainDecision`).

    Everything a card shows is here so that moving to the next one costs no request: the
    sample pages, the vote counts, the padlock and the suggestion all travel with the row.
    """
    name: str
    submission_count: int = Field(
        description="How many submissions of this domain are still pending.")
    first_submitted_on: datetime = Field(
        description="When this domain was first submitted, by whoever asked first.")
    first_submitted_by: int
    first_submitted_by_username: str
    upvotes: int = Field(
        description="Votes cast on results from this domain, ignoring a leading `www.`.")
    downvotes: int
    https: Optional[bool] = Field(
        default=None,
        description=(
            "Whether the homepage answered over TLS. `null` until the domain has been "
            "crawled - an uncrawled domain must not draw an open padlock."
        ))
    evidence_state: str = Field(
        description="`PENDING` while the domain is still being crawled, then `READY` or `FAILED`.")
    pages: list[CrawledPageSchema] = Field(
        default=[], description="The pages crawled from the domain, as shown on the card.")
    suggestion: Optional[SuggestionSchema] = Field(
        default=None,
        description="Absent until the domain has been crawled. Never a placeholder.")


class SubmissionDetailSchema(Schema):
    """Everything a moderator needs to decide one submission."""
    id: int
    name: str
    submitted_by: int
    submitted_by_username: str = ""
    submitted_on: datetime
    status: str
    rejection_reason: str = ""
    rejection_detail: str = ""
    evidence_state: str
    suggestion: Optional[SuggestionSchema] = None
    pages: list[CrawledPageSchema] = []
    signals: dict = {}
    submitter_record: dict = Field(
        default={}, description="Counts of this submitter's previously approved/rejected domains.")
    prior_decisions: dict = Field(
        default={}, description="Counts of earlier decisions on this same domain.")
    index_stats: dict = Field(
        default={}, description="What the crawler already knows about this domain.")


class DomainDecision(RejectionFieldsMixin, Schema):
    """One decision, about one domain. Lengths match the columns these are stored in.

    Addressed to the domain rather than to a submission id, because that is what a moderator
    decided about: a decision settles every submission of the name at once, so nine asks for
    the same site cannot end up half approved and half rejected.
    """
    domain: str = Field(max_length=300)
    status: str = Field(max_length=20)
    rejection_reason: str = Field(default="", max_length=20)
    rejection_detail: str = Field(default="", max_length=300)
    suggested_status: Optional[str] = Field(default=None, max_length=20)
    suggested_reason: Optional[str] = Field(default=None, max_length=20)
    suggestion_confidence: Optional[float] = None
    suggestion_model_version: Optional[str] = Field(default=None, max_length=50)


class BulkDecisionRequest(Schema):
    """A screenful of individually-made decisions, sent in one request.

    Deliberately not "accept all suggestions": each entry carries its own status, so the
    client can only send choices a moderator actually made.
    """
    decisions: list[DomainDecision]


class ModeratedDomainSchema(Schema):
    """One domain in the moderation history: the decision that currently stands on it.

    One row per domain, like the queue, and for the same reason - and it carries the
    `suggested_*` audit columns so a moderator revisiting a call can see what was on screen
    when it was made, rather than what the model would say about the domain today.
    """
    name: str
    submission_count: int
    status: str
    rejection_reason: str = ""
    rejection_detail: str = ""
    first_submitted_on: datetime
    first_submitted_by: int
    first_submitted_by_username: str
    status_changed_on: Optional[datetime] = None
    status_changed_by: Optional[int] = None
    status_changed_by_username: str = ""
    suggested_status: str = ""
    suggested_reason: str = ""
    suggestion_confidence: Optional[float] = None
    suggestion_model_version: str = ""


class ModerationHistory(Schema):
    """A page of past moderations. Same `items`/`count` shape as the queue."""
    items: list[ModeratedDomainSchema]
    count: int


class VoteRequest(Schema):
    """Request schema for voting on search results."""
    
    url: str = Field(
        description="The URL of the search result being voted on",
        example="https://example.com/article"
    )
    query: str = Field(
        description="The search query that returned this result",
        example="python tutorial"
    )
    vote_type: Literal["upvote", "downvote"] = Field(
        description="Type of vote - either 'upvote' for positive feedback or 'downvote' for negative feedback",
        example="upvote"
    )


class VoteRemoveRequest(Schema):
    """Request schema for removing a vote on a search result."""
    
    url: str = Field(
        description="The URL of the search result to remove the vote from",
        example="https://example.com/article"
    )
    query: str = Field(
        description="The search query that returned this result",
        example="python tutorial"
    )


class VoteStats(Schema):
    """Statistics for votes on a specific search result."""
    
    upvotes: int = Field(
        description="Total number of upvotes for this search result",
        example=15
    )
    downvotes: int = Field(
        description="Total number of downvotes for this search result",
        example=3
    )
    user_vote: Optional[Literal["upvote", "downvote"]] = Field(
        default=None,
        description="The current user's vote on this result, if any",
        example="upvote"
    )


class VoteStatsRequest(Schema):
    """Request schema for getting vote statistics for multiple URLs."""
    
    query: str = Field(
        description="The search query that returned these results",
        example="python tutorial"
    )
    urls: list[str] = Field(
        description="List of URLs to get vote statistics for",
        example=["https://example.com/article", "https://another-site.com/page"]
    )


class VoteResponse(Schema):
    """Response schema containing vote statistics for multiple URLs."""
    
    votes: dict[str, VoteStats] = Field(
        description="Dictionary mapping URLs to their vote statistics",
        example={
            "https://example.com/article": {
                "upvotes": 15,
                "downvotes": 3,
                "user_vote": "upvote"
            },
            "https://another-site.com/page": {
                "upvotes": 8,
                "downvotes": 1,
                "user_vote": None
            }
        }
    )


class UserVoteHistory(Schema):
    """Schema representing a user's voting history entry."""
    
    url: str = Field(
        description="The URL of the search result that was voted on",
        example="https://example.com/article"
    )
    query: str = Field(
        description="The search query that returned this result",
        example="python tutorial"
    )
    vote_type: Literal["upvote", "downvote"] = Field(
        description="The type of vote cast by the user",
        example="upvote"
    )
    timestamp: datetime = Field(
        description="When the vote was cast",
        example="2024-01-15T10:30:00Z"
    )


class ModerationQueue(Schema):
    """A page of the moderation queue. Shape matches the paginated endpoints the client
    already consumes, so `items`/`count` need no special handling.

    `count` is distinct pending *domains* - the "24 pending" in the header - not pending
    submissions.
    """
    items: list[QueueItemSchema]
    count: int
