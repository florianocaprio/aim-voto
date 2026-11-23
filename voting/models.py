from typing import ClassVar
from uuid import uuid4

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

MAX_ELECTORATE_SIZE = 2000


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
        SECRET = "secret", "Segreto"

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
    allow_delegations = models.BooleanField(default=False)
    require_presence = models.BooleanField(default=False)
    reminder_interval_hours = models.PositiveSmallIntegerField(default=24)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    cancellation_reason = models.TextField(blank=True, max_length=500)

    class Meta:
        ordering: ClassVar = ["-created_at", "-pk"]
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(mode__in=["named", "secret"]),
                name="election_valid_mode",
            ),
            models.CheckConstraint(
                condition=models.Q(opens_at__isnull=True)
                | models.Q(closes_at__gt=models.F("opens_at")),
                name="election_valid_window",
            ),
        ]

    def __str__(self):
        return self.title

    @property
    def is_secret(self):
        return self.mode == self.Mode.SECRET

    def participation_records(self):
        if self.is_secret:
            latest = SubmissionReceipt.objects.filter(
                voter_id=models.OuterRef("voter_id")
            ).order_by("-revision")
            return SubmissionReceipt.objects.filter(
                voter__election=self, pk=models.Subquery(latest.values("pk")[:1])
            ).annotate(
                submitted_at=models.F("accepted_at"),
            )
        return NamedBallot.objects.filter(voter__election=self)


class AssemblyQuorum(models.Model):
    class Basis(models.TextChoices):
        PRESENCES = "presences", "Presenze confermate"
        WITH_PROXY = (
            "with_proxy",
            "Presenze confermate e deleganti con scheda acquisita",
        )

    class Comparison(models.TextChoices):
        AT_LEAST = "at_least", "Almeno"
        GREATER = "greater", "Più di"

    election = models.OneToOneField(Election, on_delete=models.CASCADE)
    basis = models.CharField(max_length=12, choices=Basis)
    comparison = models.CharField(
        max_length=8, choices=Comparison, default=Comparison.AT_LEAST
    )
    threshold_numerator = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    threshold_denominator = models.PositiveIntegerField(
        validators=[MinValueValidator(1)], null=True, blank=True
    )
    reference = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(threshold_numerator__gte=1)
                & (
                    models.Q(threshold_denominator__isnull=True)
                    | models.Q(
                        threshold_denominator__gte=models.F("threshold_numerator")
                    )
                ),
                name="assembly_quorum_valid_threshold",
            ),
        ]

    def minimum_count(self, eligible):
        # A fractional threshold uses integer arithmetic, including at the exact boundary.
        denominator = self.threshold_denominator or 1
        scaled = (
            self.threshold_numerator * eligible
            if self.threshold_denominator
            else self.threshold_numerator
        )
        if self.comparison == self.Comparison.GREATER:
            return scaled // denominator + 1
        return (scaled + denominator - 1) // denominator


class Question(models.Model):
    class Kind(models.TextChoices):
        MOTION_REQUIRED = "motion_req", "Favorevole / Contrario / Astenuto"
        MOTION = "motion", "Favorevole / Contrario / Astenuto / Senza risposta"
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

    @property
    def requires_answer(self):
        return self.kind == self.Kind.MOTION_REQUIRED


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
    member = models.ForeignKey(
        "members.Member", on_delete=models.PROTECT, null=True, blank=True
    )
    external_id = models.CharField(max_length=64)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    section = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering: ClassVar = ["last_name", "first_name", "pk"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["election", "member"], name="one_place_per_member"
            ),
            models.UniqueConstraint(
                fields=["election", "external_id"], name="one_matricola_per_election"
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
    cast_by = models.ForeignKey(
        ElectionVoter, null=True, on_delete=models.PROTECT, related_name="proxy_ballots"
    )


class SecretBallot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    election = models.ForeignKey(Election, on_delete=models.PROTECT)
    ciphertext = models.BinaryField(editable=False)


class VoterPresence(models.Model):
    class Source(models.TextChoices):
        EXPLICIT = "explicit", "Conferma esplicita"
        BALLOT = "ballot", "Scheda acquisita"

    voter = models.OneToOneField(
        ElectionVoter, on_delete=models.CASCADE, related_name="presence"
    )
    confirmed_at = models.DateTimeField()
    source = models.CharField(max_length=8, choices=Source, default=Source.EXPLICIT)


class Delegation(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Da verificare"
        APPROVED = "approved", "Approvata"
        REJECTED = "rejected", "Rifiutata"
        REVOKED = "revoked", "Revocata"

    grantor = models.ForeignKey(
        ElectionVoter, on_delete=models.CASCADE, related_name="delegations_given"
    )
    delegate = models.ForeignKey(
        ElectionVoter, on_delete=models.CASCADE, related_name="delegations_received"
    )
    state = models.CharField(max_length=8, choices=State, default=State.PENDING)
    requested_at = models.DateTimeField()

    class Meta:
        ordering: ClassVar = ["-requested_at", "-pk"]
        constraints: ClassVar = [
            models.CheckConstraint(
                condition=~models.Q(grantor=models.F("delegate")),
                name="delegation_distinct_voters",
            ),
            models.UniqueConstraint(
                fields=["grantor"],
                condition=models.Q(state__in=["pending", "approved"]),
                name="delegation_one_active_per_grantor",
            ),
        ]


class DelegationAttachment(models.Model):
    delegation = models.OneToOneField(
        Delegation, on_delete=models.CASCADE, related_name="attachment"
    )
    filename = models.CharField(max_length=150)
    mime_type = models.CharField(max_length=20)
    content = models.BinaryField(editable=False)


class SubmissionReceipt(models.Model):
    voter = models.ForeignKey(
        ElectionVoter, on_delete=models.PROTECT, related_name="receipts"
    )
    request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    revision = models.PositiveIntegerField()
    accepted_at = models.DateTimeField()
    cast_by = models.ForeignKey(
        ElectionVoter,
        null=True,
        on_delete=models.PROTECT,
        related_name="proxy_receipts",
    )

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
