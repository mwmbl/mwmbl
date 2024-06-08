from django.db import models
from django.contrib.auth.models import AbstractUser


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
