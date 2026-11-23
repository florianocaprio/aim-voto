import uuid

from django.conf import settings
from django.db import models


class Member(models.Model):
    external_id = models.CharField("ID socio", max_length=64, unique=True)
    first_name = models.CharField("Nome", max_length=100)
    last_name = models.CharField("Cognome", max_length=100)
    email = models.EmailField("Email")
    phone = models.CharField("Cellulare", max_length=32, blank=True)
    section = models.CharField("Sezione", max_length=100)

    class Meta:
        ordering = ("last_name", "first_name", "external_id")

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.external_id})"


class ImportBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    election = models.ForeignKey("voting.Election", null=True, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    headings = models.JSONField(default=list)
    records = models.JSONField(default=list)
    blank_rows = models.PositiveIntegerField(default=0)
    mapping = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
