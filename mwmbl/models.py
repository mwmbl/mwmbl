import secrets

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from ninja import ModelSchema
from ninja.orm import create_schema

from mwmbl.usernames import generate_username


class MwmblUser(AbstractUser):
    class Tier(models.TextChoices):
        FREE    = "free",    "Free"
        STARTER = "starter", "Starter"
        PRO     = "pro",     "Pro"

    TIER_MONTHLY_LIMITS = {
        Tier.FREE:    1_000,
        Tier.STARTER: 10_000,
        Tier.PRO:     50_000,
    }

    tier = models.CharField(
        max_length=20,
        choices=Tier.choices,
        default=Tier.FREE,
    )


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


class DomainSubmission(models.Model):
    class Meta:
        permissions = [
            ("change_domain_submission_status", "Can change the domain submission status"),
        ]
        indexes = [
            models.Index(fields=['submitted_on']),
        ]

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

    name = models.CharField(max_length=300)
    submitted_by = models.ForeignKey(MwmblUser, on_delete=models.CASCADE, related_name="domain_submissions")
    submitted_on = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=[(k, v) for k, v in DOMAIN_SUBMISSION_STATUS.items()], default="PENDING")
    status_changed_by = models.ForeignKey(MwmblUser, on_delete=models.CASCADE, null=True, blank=True, related_name="domain_submissions_changed")
    status_changed_on = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=20, choices=[(k, v) for k, v in DOMAIN_REJECTION_REASON.items()], blank=True)
    rejection_detail = models.CharField(max_length=300, blank=True)


def random_api_key():
    return secrets.token_urlsafe(64)


class ApiKey(models.Model):
    class Scope(models.TextChoices):
        CRAWL  = "crawl",  "Crawl"
        SEARCH = "search", "Search"

    user       = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    key        = models.CharField(max_length=300, unique=True, default=random_api_key)
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
    
    class Meta:
        unique_together = ['user', 'url', 'query']  # One vote per user per result per query
        indexes = [
            models.Index(fields=['url', 'query']),
            models.Index(fields=['timestamp']),
        ]
