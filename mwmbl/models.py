import hashlib
import secrets

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from ninja import ModelSchema
from ninja.orm import create_schema

from mwmbl.usernames import generate_username
from mwmbl.utils import bare_host


class MwmblUser(AbstractUser):
    pass


class UserCuration(models.Model):
    """
    Deprecated - use Curation instead
    """
    user = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    timestamp = models.DateTimeField()
    url = models.CharField(max_length=300)
    results = models.JSONField()
    curation_type = models.CharField(max_length=20)
    curation = models.JSONField()


class Curation(models.Model):
    user = models.ForeignKey(MwmblUser, on_delete=models.CASCADE, null=True)
    timestamp = models.DateTimeField()
    query = models.CharField(max_length=300)

    # The original results as stored in the index
    original_index_results = models.JSONField(default=list)

    # The original results that the user saw. May include results from Google via the extension.
    original_results = models.JSONField()
    new_results = models.JSONField()
    num_changes = models.IntegerField(default=0)


class FlagCuration(models.Model):
    class Meta:
        permissions = [
            ("change_flag_status", "Can change the flag status (approve or reject)"),
        ]

    FLAG_TYPES = {
        "RELEVANCE": "The curation is unlikely to be useful to a large number of users",
        "LANGUAGE": "The curation is for a query in an unsupported language",
        "PROMOTION": "The curation promotes a specific website or product",
        "OFFENSIVE": "The curation contains offensive content",
        "OTHER": "Other",
    }

    FLAG_STATUS = {
        "PENDING": "The flag has been submitted and is awaiting review",
        "REJECTED": "The flag has been rejected",
        "ACCEPTED": "The flag has been accepted and the curation has been reverted",
    }

    user = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    timestamp = models.DateTimeField()
    curation = models.ForeignKey(Curation, on_delete=models.CASCADE, related_name="flag_curation_set")
    flag = models.CharField(max_length=20, choices=[(k, v) for k, v in FLAG_TYPES.items()])
    reason = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=20, choices=[(k, v) for k, v in FLAG_STATUS.items()], default="PENDING")


class OldIndex(models.Model):
    index_path = models.CharField(max_length=300, primary_key=True)
    last_copied_time = models.DateTimeField(null=True, blank=True)
    last_page_copied = models.IntegerField(null=True, blank=True)


# Module level rather than class attributes because the check constraints below need them,
# and a nested Meta cannot see names from the class body it sits in. The class attributes are
# kept as aliases: they are what the API schemas and management commands read.
DOMAIN_SUBMISSION_STATUS = {
    "PENDING": "The domain submission is awaiting review",
    "APPROVED": "The domain submission has been approved",
    "REJECTED": "The domain submission has been rejected",
}

DOMAIN_REJECTION_REASON = {
    "SPAM": "The domain submission was rejected because it was spam",
    "OFFENSIVE": "The domain submission was rejected because it was offensive",
    "LANGUAGE": "The domain is in an unsupported language",
    "OTHER": "The domain submission was rejected for another reason",
}


class DomainSubmission(models.Model):
    class Meta:
        permissions = [
            ("change_domain_submission_status", "Can change the domain submission status"),
        ]
        indexes = [
            models.Index(fields=['submitted_on']),
            # The moderation queue asks "has this domain been decided before?" and "has this
            # submitter?" once per row it renders, as correlated subqueries. Both are lookups
            # into this table by a decided status. See mwmbl.moderation.suggest.
            models.Index(fields=['name', 'status']),
            models.Index(fields=['submitted_by', 'status']),
        ]
        constraints = [
            # `choices` is documentation, not enforcement: it is checked by full_clean(),
            # which save() never calls, and it produces no database constraint at all. A
            # moderation client that posted the action it shows the moderator - "APPROVE" for
            # "APPROVED" - therefore wrote that straight into the column, where it matched
            # neither the pending queue nor the approved set the curated domains are built
            # from, and the decision disappeared. 1,785 rows were written that way before
            # migration 0037 repaired them and added these.
            models.CheckConstraint(
                condition=models.Q(status__in=list(DOMAIN_SUBMISSION_STATUS)),
                name="domain_submission_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(rejection_reason__in=[""] + list(DOMAIN_REJECTION_REASON)),
                name="domain_submission_rejection_reason_valid",
            ),
        ]

    DOMAIN_SUBMISSION_STATUS = DOMAIN_SUBMISSION_STATUS
    DOMAIN_REJECTION_REASON = DOMAIN_REJECTION_REASON

    name = models.CharField(max_length=300)
    submitted_by = models.ForeignKey(MwmblUser, on_delete=models.CASCADE, related_name="domain_submissions")
    submitted_on = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=[(k, v) for k, v in DOMAIN_SUBMISSION_STATUS.items()], default="PENDING")
    status_changed_by = models.ForeignKey(MwmblUser, on_delete=models.CASCADE, null=True, blank=True, related_name="domain_submissions_changed")
    status_changed_on = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=20, choices=[(k, v) for k, v in DOMAIN_REJECTION_REASON.items()], blank=True)
    rejection_detail = models.CharField(max_length=300, blank=True)

    # What the suggestion model was showing when the moderator made this decision. Deliberately
    # separate storage from the live suggestion on DomainEvidence: a retrain rewrites that row,
    # and sharing the columns would silently rewrite the record of what was actually on screen.
    # These are what make the suggestions' influence measurable: a retrain trains on decisions
    # that were themselves made with a suggestion on screen, and without a record of what was
    # shown there is no way to see that happening. The retrain does not yet weight rows by it -
    # see mwmbl.moderation.training_data - it reports it.
    suggested_status = models.CharField(max_length=20, blank=True)
    suggested_reason = models.CharField(max_length=20, blank=True)
    suggestion_confidence = models.FloatField(null=True, blank=True)
    suggestion_model_version = models.CharField(max_length=50, blank=True)


def random_api_key():
    """Kept for migration compatibility (0010_apikey references this by name)."""
    return secrets.token_urlsafe(64)


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, key_hash). Store only the hash; return raw_key to the user once."""
    raw = secrets.token_urlsafe(64)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


class ApiKey(models.Model):
    class Scope(models.TextChoices):
        CRAWL  = "crawl",  "Crawl"
        SEARCH = "search", "Search"

    user       = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    key        = models.CharField(max_length=64, unique=True)  # stores SHA-256 hash of the raw key
    created_on = models.DateTimeField(auto_now_add=True)
    name       = models.CharField(max_length=100, blank=True, default="")
    scopes     = ArrayField(
        models.CharField(max_length=20, choices=Scope.choices),
        default=list,
    )


class WasmEvaluationJob(models.Model):
    EVALUATION_STATUS = {
        "PENDING": "The evaluation job is pending",
        "VALIDATED": "The WASM file has been validated",
        "RUNNING": "The evaluation is currently running",
        "COMPLETED": "The evaluation has completed successfully",
        "FAILED": "The evaluation has failed",
    }

    user = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    wasm_file = models.BinaryField()  # Store WASM bytes directly
    status = models.CharField(max_length=20, choices=[(k, v) for k, v in EVALUATION_STATUS.items()], default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    results = models.JSONField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)


class UsageBucket(models.Model):
    """Records a user's API usage for a specific calendar month."""
    user = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    year = models.IntegerField()
    month = models.IntegerField()
    count = models.IntegerField(default=0)
    # How many billable (over-the-free-allowance) requests have already been
    # ingested to Polar as usage events for this month. Lets the reporting job
    # send only the incremental delta each run, idempotently.
    reported_overage = models.IntegerField(default=0)

    class Meta:
        unique_together = [('user', 'year', 'month')]
        indexes = [
            models.Index(fields=['year', 'month']),
        ]


class UserBilling(models.Model):
    user = models.OneToOneField(MwmblUser, on_delete=models.CASCADE, related_name="billing")
    polar_customer_id = models.CharField(max_length=100, blank=True, default="")
    polar_subscription_id = models.CharField(max_length=100, blank=True, default="")
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    # Maximum amount (in cents) the account may be billed per month for metered
    # usage beyond the free allowance. 0 = free-tier-only (hard capped).
    max_monthly_spend_cents = models.IntegerField(default=0)


class AgreementType(models.TextChoices):
    TERMS_OF_SERVICE_GUI = "TERMS_OF_SERVICE_GUI", "Terms of Service (GUI)"
    TERMS_OF_SERVICE_API = "TERMS_OF_SERVICE_API", "Terms of Service (API)"


class UserAgreement(models.Model):
    # SET_NULL rather than CASCADE so the audit record survives account deletion.
    user = models.ForeignKey(
        MwmblUser, on_delete=models.SET_NULL, null=True, related_name="agreements"
    )
    agreement_type = models.CharField(max_length=50, choices=AgreementType.choices)
    version_id = models.CharField(max_length=100)
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "agreement_type", "-accepted_at"]),
        ]


class MarketingSource(models.TextChoices):
    GUI = "GUI", "Consumer site (mwmbl.org)"
    API = "API", "Developer site (developer.mwmbl.org)"


class MarketingConsent(models.Model):
    """
    Append-only audit trail of marketing email consent decisions.

    Every opt-in and opt-out writes a new row; the current state for a source is
    the `opted_in` of the latest row. Mirrors UserAgreement.
    """
    # SET_NULL (not CASCADE) so the consent proof survives account deletion,
    # matching UserAgreement.
    user = models.ForeignKey(
        MwmblUser, on_delete=models.SET_NULL, null=True, related_name="marketing_consents"
    )
    source = models.CharField(max_length=10, choices=MarketingSource.choices)
    opted_in = models.BooleanField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "source", "-timestamp"]),
        ]


class SearchResultVote(models.Model):
    VOTE_TYPES = {
        "upvote": "User upvoted this result",
        "downvote": "User downvoted this result",
    }
    
    user = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    url = models.URLField(max_length=500)  # The URL of the search result
    query = models.CharField(max_length=300)  # The search query context
    vote_type = models.CharField(max_length=10, choices=[(k, v) for k, v in VOTE_TYPES.items()])
    timestamp = models.DateTimeField(auto_now_add=True)
    # The host the URL belongs to, derived on save. Denormalised because the moderation queue
    # rolls votes up per domain, and it orders and paginates thousands of pending domains on
    # the result - which has to happen in the database, and cannot parse a URLField there.
    domain = models.CharField(max_length=300, blank=True)

    def save(self, *args, **kwargs):
        self.domain = bare_host(self.url)
        super().save(*args, **kwargs)

    class Meta:
        unique_together = ['user', 'url', 'query']  # One vote per user per result per query
        indexes = [
            models.Index(fields=['url', 'query']),
            models.Index(fields=['timestamp']),
            # The per-domain rollup the moderation queue reads: count this domain's upvotes,
            # count its downvotes. See mwmbl.moderation.suggest.annotate_votes.
            models.Index(fields=['domain', 'vote_type']),
        ]


class SuperSearchImpression(models.Model):
    """One Super Search request: which sources were available, which were queried,
    the features they were selected on, and the implicit reward each earned.

    Feeds the offline feature-selection / policy-tuning harness and provides
    durable training data for the contextual bandit. Deliberately does not
    store the query text, to avoid persisting user search history.
    """
    candidates = models.JSONField(default=list)   # all selectable source names (action space)
    selected = models.JSONField(default=list)     # sources actually queried
    features = models.JSONField(default=dict)     # {source: [feature vector]} for selected sources
    rewards = models.JSONField(default=dict)      # {source: reward in [0, 1]}
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['timestamp']),
        ]


class SourceProvenance(models.Model):
    """Which Super Search source a URL was (transitively) discovered from.

    Written for each URL a source returns, and propagated onto links found on
    pages later crawled from those URLs, so source usefulness can be judged
    offline including for descendant pages. Deliberately does not store the
    query text, to avoid persisting user search history.
    """
    url = models.URLField(max_length=500, unique=True)   # first source wins, matches source_by_url semantics
    source = models.CharField(max_length=128)            # super-search source name (e.g. "gov.uk")
    parent_url = models.URLField(max_length=500, null=True, blank=True)  # page this URL was found on (null = direct result)
    depth = models.IntegerField(default=0)               # 0 = direct super-search result; +1 per crawl hop
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['source']),
            models.Index(fields=['timestamp']),
        ]


class DomainEvidence(models.Model):
    """Crawl evidence and the precomputed suggestion for a submitted domain.

    Both the moderator's detail panel and the suggestion model read these rows, so what the
    moderator is shown and what the model judged can never disagree. Everything here is
    computed by a background task at submission time: the moderation queue is a plain indexed
    read, with no inference on the request path.

    Keyed on the domain rather than the submission because domains get resubmitted - 615 of
    6,949 distinct names in the last judgments export had more than one submission - and the
    crawl is about the domain, not about who asked for it.

    What the queue orders on is *not* stored here. The suggestion a moderator sees depends on
    rows this one knows nothing about - the submitter's record, and whether the domain has
    since been decided - so the sort key is computed per query in SQL by
    mwmbl.moderation.suggest.annotate_queue. Storing one would only ever be the answer to a
    question nobody asked in that form.
    """

    class State(models.TextChoices):
        PENDING = "PENDING", "Queued for crawling"
        READY = "READY", "Crawled and scored"
        FAILED = "FAILED", "Crawling failed"

    domain = models.CharField(max_length=300, unique=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    fetched_at = models.DateTimeField(null=True, blank=True)

    # Crawl results.
    http_status = models.IntegerField(null=True, blank=True)
    final_domain = models.CharField(max_length=300, blank=True)   # after redirects
    error = models.CharField(max_length=100, blank=True)          # RobotsDenied, AbortError, ...
    pages = models.JSONField(default=list)      # [{url, status, title, extract}], up to 3
    signals = models.JSONField(default=dict)    # derived: lang, has_links, ad_script_count, ...

    # Precomputed suggestion. The queue reads these columns; it never calls the model.
    suggested_action = models.CharField(max_length=10, blank=True)   # APPROVE | REJECT | UNSURE
    suggested_reason = models.CharField(max_length=20, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    # How sure we are of the *reason*, and where it came from - both separate from the numbers
    # above, because a confident rejection can carry a barely-held guess at why. Storing them
    # is what lets the API show the reason with its own confidence instead of the rejection's,
    # and keeps `derived` (a reason class learned from blocklists rather than from moderator
    # decisions) visible to the moderator it is a caveat for.
    reason_confidence = models.FloatField(null=True, blank=True)
    reason_source = models.CharField(max_length=10, blank=True)   # rule | model | derived
    evidence = models.JSONField(default=list)   # cached rule + model evidence items
    model_version = models.CharField(max_length=50, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["suggested_action", "suggested_reason"]),
        ]

    def __str__(self):
        return f"{self.domain} ({self.state})"


class ModerationModelArtifact(models.Model):
    """A trained moderation suggester, stored where a deploy cannot overwrite it.

    The artifact used to live in the source tree, which meant every deploy silently reverted
    the monthly retrain to whatever was committed, and each worker replica kept its own
    divergent copy. The database is the one place all the workers already agree on.

    Rows accumulate: the newest is served, and the ones behind it are the record of what was
    serving when a given decision was made. Versions are dated, so a second retrain on the
    same day replaces its own row rather than adding one. ``metrics`` travels in the same row
    as the model it describes, so the retrain gate cannot compare a candidate against numbers
    that belong to a different artifact. At a monthly retrain that is ~12 MB a year.

    The model bundled in mwmbl/moderation/artifacts is the warm start for a database with no
    rows yet; nothing ever writes to it. See mwmbl.moderation.model.
    """

    version = models.CharField(max_length=50, unique=True)
    model = models.BinaryField()                # joblib pickle of a ModerationModel, ~1 MB
    metrics = models.JSONField(default=dict)    # what train.evaluate measured for this model
    created_on = models.DateTimeField(default=timezone.now)

    class Meta:
        get_latest_by = "created_on"
        indexes = [
            models.Index(fields=["-created_on"]),
        ]

    def __str__(self):
        return f"{self.version} ({self.created_on:%Y-%m-%d})"
