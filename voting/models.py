from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone


class Election(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Bozza"
        SCHEDULED = "scheduled", "Programmata"
        OPEN = "open", "Aperta"
        CLOSED = "closed", "Chiusa"
        TALLIED = "tallied", "Scrutinata"
        CANCELLED = "cancelled", "Annullata"

    class Mode(models.TextChoices):
        NAMED = "named", "Palese (nominativo)"

    title = models.CharField(max_length=180)
    description = models.TextField(blank=True, max_length=6000)
    mode = models.CharField(max_length=12, choices=Mode, default=Mode.NAMED)
    status = models.CharField(
        max_length=12, choices=Status, default=Status.DRAFT, db_index=True
    )
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField()
    opened_at = models.DateTimeField(null=True, editable=False)
    ended_at = models.DateTimeField(null=True, editable=False)
    allow_replacement = models.BooleanField(default=True)
    reminder_interval_hours = models.PositiveSmallIntegerField(default=24)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    cancellation_reason = models.TextField(blank=True, max_length=500)

    class Meta:
        ordering: ClassVar = ["-created_at", "-pk"]
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(mode="named"), name="election_named_only"
            ),
            models.CheckConstraint(
                condition=models.Q(opens_at__isnull=True)
                | models.Q(closes_at__gt=models.F("opens_at")),
                name="election_valid_window",
            ),
        ]

    def __str__(self):
        return self.title


class Question(models.Model):
    class Kind(models.TextChoices):
        MOTION = "motion", "Favorevole / Contrario / Astenuto"
        SINGLE = "single", "Scelta singola"
        MULTIPLE = "multiple", "Scelta multipla"
        CANDIDATES = "candidates", "Candidati"

    election = models.ForeignKey(
        Election, on_delete=models.CASCADE, related_name="questions"
    )
    text = models.CharField(max_length=500)
    kind = models.CharField(max_length=16, choices=Kind)
    max_choices = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering: ClassVar = ["pk"]


class Choice(models.Model):
    question = models.ForeignKey(
        Question, on_delete=models.CASCADE, related_name="choices"
    )
    text = models.CharField(max_length=200)

    class Meta:
        ordering: ClassVar = ["pk"]


class ElectionDocument(models.Model):
    election = models.ForeignKey(
        Election, on_delete=models.CASCADE, related_name="documents"
    )
    name = models.CharField(max_length=150)
    content = models.BinaryField(editable=False)


class ElectionVoter(models.Model):
    election = models.ForeignKey(
        Election, on_delete=models.CASCADE, related_name="voters"
    )
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT)
    external_id = models.CharField(max_length=64)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    section = models.CharField(max_length=100)

    class Meta:
        ordering: ClassVar = ["last_name", "first_name", "pk"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["election", "member"], name="one_place_per_member"
            ),
        ]


class VotingCredential(models.Model):
    voter = models.OneToOneField(
        ElectionVoter, on_delete=models.CASCADE, related_name="credential"
    )
    digest = models.CharField(max_length=64, unique=True, editable=False)
    ciphertext = models.BinaryField(editable=False)
    version = models.PositiveIntegerField(default=1)


class NamedBallot(models.Model):
    voter = models.OneToOneField(
        ElectionVoter, on_delete=models.PROTECT, related_name="ballot"
    )
    answers = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    submitted_at = models.DateTimeField(default=timezone.now)


class SubmissionReceipt(models.Model):
    voter = models.ForeignKey(
        ElectionVoter, on_delete=models.PROTECT, related_name="receipts"
    )
    request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    revision = models.PositiveIntegerField()
    accepted_at = models.DateTimeField()

    class Meta:
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["voter", "request_id"], name="unique_ballot_request"
            ),
            models.UniqueConstraint(
                fields=["voter", "revision"], name="unique_ballot_revision"
            ),
        ]


class AuditEvent(models.Model):
    action = models.CharField(max_length=48)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, blank=True, null=True, on_delete=models.SET_NULL
    )
    election = models.ForeignKey(
        Election, blank=True, null=True, on_delete=models.SET_NULL
    )
    voter = models.ForeignKey(
        ElectionVoter, blank=True, null=True, on_delete=models.SET_NULL
    )
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering: ClassVar = ["-created_at", "-pk"]


class RateBucket(models.Model):
    key = models.CharField(primary_key=True, max_length=64)
    since = models.DateTimeField(db_index=True)
    hits = models.PositiveIntegerField(default=0)
