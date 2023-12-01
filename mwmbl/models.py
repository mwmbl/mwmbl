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
    user = models.ForeignKey(MwmblUser, on_delete=models.CASCADE)
    timestamp = models.DateTimeField()
    url = models.CharField(max_length=300)
    original_results = models.JSONField()
    new_results = models.JSONField()
    num_changes = models.IntegerField(default=0)
