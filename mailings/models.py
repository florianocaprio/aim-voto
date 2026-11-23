import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class MailKind(models.TextChoices):
    INVITE = "invite", "Inviti"
    EARLY = "early", "Apertura per deleghe"
    VOTE_OPEN = "voteopen", "Apertura del voto"
    REMINDER = "reminder", "Solleciti ai non votanti"
    RESEND = "resend", "Reinvio individuale"
    PROXY = "proxy", "Link di voto per delega"
    RESULTS = "results", "Risultati della votazione"


class CampaignState(models.TextChoices):
    PREVIEW = "preview", "Anteprima"
    CONFIRMED = "confirmed", "Confermata"
    CANCELLED = "cancelled", "Annullata"


class DeliveryState(models.TextChoices):
    PREVIEW = "preview", "Da confermare"
    QUEUED = "queued", "In coda"
    SENDING = "sending", "In invio"
    ACCEPTED = "accepted", "Accettato da SMTP"
    FAILED = "failed", "Fallito"
    UNCERTAIN = "uncertain", "Esito incerto"
    SKIPPED = "skipped", "Escluso"
    CANCELLED = "cancelled", "Annullato"


class DeliveryReason(models.TextChoices):
    CLOSED = "closed", "Consultazione chiusa o scaduta"
    NOT_OPEN = "not_open", "Consultazione in attesa di apertura"
    RESULTS_UNPUBLISHED = "results_unpublished", "Scrutinio non pubblicato"
    VOTED = "voted", "Voto già acquisito"
    INVALID_CREDENTIAL = "invalid_credential", "Credenziale assente o non valida"
    MISSING_EMAIL = "missing_email", "Indirizzo email mancante"
    INVALID_EMAIL = "invalid_email", "Indirizzo diverso da example.invalid o non valido"
    FREQUENCY = "frequency", "Intervallo fra solleciti ancora in corso"
    PENDING = "pending", "Invio già in corso o in coda"
    ALREADY_INVITED = "already_invited", "Invito già inviato o con esito incerto"
    EARLY_ENDED = "early_ended", "Fase anticipata per le deleghe terminata"
    CANCELLED = "cancelled", "Annullato dall’amministratore"
    TRANSIENT = "smtp_temporary", "Errore temporaneo SMTP"
    PERMANENT = "smtp_permanent", "Errore permanente SMTP"
    UNKNOWN = "smtp_unknown", "Riscontro SMTP assente o ambiguo"
    INTERRUPTED = "interrupted", "Tentativo interrotto con esito sconosciuto"
    INVALID_DELEGATION = (
        "invalid_delegation",
        "Delega assente, revocata o non approvata",
    )


class InvitationTemplate(models.Model):
    election = models.OneToOneField(
        "voting.Election", on_delete=models.CASCADE, related_name="invitation_template"
    )
    subject = models.CharField(max_length=140)
    body = models.TextField()


class ReminderCampaign(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(
        "voting.Election", on_delete=models.CASCADE, related_name="campaigns"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    kind = models.CharField(max_length=8, choices=MailKind.choices)
    state = models.CharField(
        max_length=9, choices=CampaignState.choices, default=CampaignState.PREVIEW
    )
    subject = models.CharField(max_length=140)
    body = models.TextField()
    section = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = (
            models.UniqueConstraint(
                fields=("election",),
                condition=Q(kind=MailKind.RESULTS),
                name="mailings_one_results_per_election",
            ),
            models.UniqueConstraint(
                fields=("election", "kind"),
                condition=Q(kind__in=(MailKind.EARLY, MailKind.VOTE_OPEN)),
                name="mailings_one_notice_per_opening",
            ),
        )


class EmailDelivery(models.Model):
    campaign = models.ForeignKey(
        ReminderCampaign, on_delete=models.CASCADE, related_name="deliveries"
    )
    voter = models.ForeignKey(
        "voting.ElectionVoter", on_delete=models.CASCADE, related_name="deliveries"
    )
    delegation = models.OneToOneField(
        "voting.Delegation",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notice",
    )
    kind = models.CharField(max_length=8, choices=MailKind.choices)
    state = models.CharField(max_length=9, choices=DeliveryState.choices)
    reason = models.CharField(max_length=24, choices=DeliveryReason.choices, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    @property
    def smtp_message_id(self):
        return f"<aim-voto-{self.campaign_id.hex}-{self.pk}@example.invalid>"

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("campaign", "voter"), name="mailings_one_per_campaign_voter"
            ),
            models.UniqueConstraint(
                fields=("voter", "kind"),
                condition=Q(state__in=(DeliveryState.QUEUED, DeliveryState.SENDING))
                & Q(delegation__isnull=True),
                name="mailings_one_pending_per_kind",
            ),
            models.CheckConstraint(
                condition=Q(kind=MailKind.PROXY, delegation__isnull=False)
                | (~Q(kind=MailKind.PROXY) & Q(delegation__isnull=True)),
                name="mailings_proxy_requires_delegation",
            ),
        )
        indexes = (
            models.Index(
                fields=("state", "available_at"), name="mailings_next_delivery"
            ),
            models.Index(
                fields=("voter", "kind", "state"), name="mailings_voter_history"
            ),
        )


class DispatchSchedule(models.Model):
    id = models.PositiveSmallIntegerField(default=1, primary_key=True)
    next_at = models.DateTimeField(default=timezone.now)
