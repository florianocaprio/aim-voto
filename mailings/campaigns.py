from datetime import timedelta
from zoneinfo import ZoneInfo

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone

from voting.models import Election, ElectionVoter
from voting.services import audit, database_now, is_open, lock_election, personal_url

from .models import (
    CampaignState,
    DeliveryReason,
    DeliveryState,
    EmailDelivery,
    InvitationTemplate,
    MailKind,
    ReminderCampaign,
)

PENDING = (DeliveryState.QUEUED, DeliveryState.SENDING)
POSSIBLY_SENT = (DeliveryState.ACCEPTED, DeliveryState.UNCERTAIN)
PREVIEW_LIFETIME = timedelta(minutes=30)
ROME = ZoneInfo("Europe/Rome")
INVITATION_TEMPLATE_STATES = (Election.Status.DRAFT, Election.Status.SCHEDULED)


def recipients(election, kind, *, rows=None):
    history = EmailDelivery.objects.filter(voter_id=OuterRef("pk"), kind=kind)
    if rows is not None:
        history = history.exclude(campaign=rows)
    reminder_history = EmailDelivery.objects.filter(
        voter_id=OuterRef("pk"),
        kind=MailKind.REMINDER,
        state=DeliveryState.ACCEPTED,
    )
    query = ElectionVoter.objects.filter(election=election).select_related("credential")
    return query.annotate(
        already_voted=Exists(
            election.participation_records().filter(voter_id=OuterRef("pk"))
        ),
        delivery_pending=Exists(history.filter(state__in=PENDING)),
        last_delivery=Subquery(
            history.filter(state__in=POSSIBLY_SENT)
            .order_by("-finished_at")
            .values("finished_at")[:1]
        ),
        last_reminder=Subquery(
            reminder_history.order_by("-finished_at").values("finished_at")[:1]
        ),
        accepted_reminders=Coalesce(
            Subquery(
                reminder_history.order_by()
                .values("voter_id")
                .annotate(total=Count("pk"))
                .values("total")
            ),
            0,
        ),
    ).order_by("last_name", "first_name", "pk")


def window_exclusion(election, kind, now):
    if kind == MailKind.RESULTS:
        return (
            ""
            if election.status == Election.Status.TALLIED
            else DeliveryReason.RESULTS_UNPUBLISHED
        )
    if not is_open(election, now):
        if (
            election.status in (Election.Status.DRAFT, Election.Status.SCHEDULED)
            and election.closes_at > now
        ):
            return DeliveryReason.NOT_OPEN
        return DeliveryReason.CLOSED
    return ""


def exclusion(election, kind, voter, now):
    reason = window_exclusion(election, kind, now)
    if reason:
        return reason
    if kind == MailKind.REMINDER and voter.already_voted:
        return DeliveryReason.VOTED
    if not voter.email:
        return DeliveryReason.MISSING_EMAIL
    if not usable_address(voter.email):
        return DeliveryReason.INVALID_EMAIL
    if kind == MailKind.RESULTS:
        return ""
    if getattr(voter, "credential", None) is None:
        return DeliveryReason.INVALID_CREDENTIAL
    if kind != MailKind.PROXY and voter.delivery_pending:
        return DeliveryReason.PENDING
    if voter.last_delivery is not None:
        if kind == MailKind.INVITE:
            return DeliveryReason.ALREADY_INVITED
        if kind == MailKind.REMINDER:
            next_allowed = voter.last_delivery + timedelta(
                hours=election.reminder_interval_hours
            )
            if now < next_allowed:
                return DeliveryReason.FREQUENCY
    return ""


def usable_address(address):
    if not address.isascii() or address.rsplit("@", 1)[-1].lower() != "example.invalid":
        return False
    try:
        validate_email(address)
    except ValidationError:
        return False
    return True


def active_link(voter):
    try:
        return personal_url(voter)
    except (ValidationError, ObjectDoesNotExist, InvalidToken):
        return None


def render_body(campaign, voter, url, *, represented=None):
    if campaign.kind == MailKind.RESULTS:
        return campaign.body
    deadline = timezone.localtime(campaign.election.closes_at, ROME).strftime(
        "%d/%m/%Y alle %H:%M %Z"
    )
    if represented is not None:
        instructions = (
            f"Scheda per: {represented.first_name} {represented.last_name}\n"
            f"Il tuo link per questa delega: {url}\n"
            "Il link apre solo la scheda del socio rappresentato. Conserva il link e non inoltrarlo.\n"
        )
    else:
        instructions = (
            f"Il tuo link personale: {url}\n\nConserva il link e non inoltrarlo.\n"
        )
        if campaign.election.allow_delegations:
            instructions += (
                "Dal link personale puoi aprire Presenza e deleghe. Lì puoi presentare una delega "
                "ricevuta allegando la domanda compilata in PDF o JPG.\n"
            )
        if campaign.election.require_presence:
            instructions += (
                "Prima di aprire la scheda ti sarà chiesto di confermare la presenza.\n"
            )
            if campaign.election.allow_delegations:
                instructions += "Puoi anche cercare un avente diritto e conferire direttamente la delega.\n"
    return (
        f"Ciao {voter.first_name} {voter.last_name},\n\n"
        f"{campaign.body}\n\n"
        f"Consultazione: {campaign.election.title}\n"
        f"Modalità di voto: {campaign.election.get_mode_display()}\n"
        f"Scadenza: {deadline} (Europe/Rome)\n"
        f"{instructions}"
        f"Assistenza Angeli in Moto ODV: {settings.SUPPORT_EMAIL}\n"
    )


@transaction.atomic
def save_invitation_template(election_id, actor, fields):
    election = lock_election(election_id)
    if election.status not in INVITATION_TEMPLATE_STATES:
        raise ValidationError(
            "Lo stato della consultazione è cambiato. Riapri la pagina degli inviti."
        )
    InvitationTemplate.objects.update_or_create(
        election=election,
        defaults={"subject": fields["subject"], "body": fields["body"]},
    )
    audit("modello_invito_salvato", actor=actor, election=election)


def enqueue_proxy_notice(election, delegation, actor):
    campaign = ReminderCampaign.objects.create(
        election=election,
        author=actor,
        kind=MailKind.PROXY,
        subject="Link di voto per delega AIM Voto",
        body=(
            "Ti è stata conferita una delega di voto."
            if actor is None
            else "La tua richiesta di delega è stata approvata."
        ),
        state=CampaignState.CONFIRMED,
        confirmed_at=timezone.now(),
    )
    EmailDelivery.objects.create(
        campaign=campaign,
        voter=delegation.delegate,
        delegation=delegation,
        kind=MailKind.PROXY,
        state=DeliveryState.QUEUED,
    )


def enqueue_results(election, actor, now, data):
    lines = [
        f"Consultazione: {election.title}",
        f"Modalità di voto: {election.get_mode_display()}",
        f"Aventi diritto: {data['eligible']}",
        f"Votanti: {data['total']} (schede acquisite, incluse le deleghe)",
    ]
    for index, row in enumerate(data["rows"], start=1):
        lines.extend(("", f"Quesito {index}: {row['question']}"))
        lines.extend(f"{label}: {count}" for label, count in row["choices"])
        lines.append(f"Senza risposta: {row['omitted']}")
    campaign = ReminderCampaign.objects.create(
        election=election,
        author=actor,
        kind=MailKind.RESULTS,
        state=CampaignState.CONFIRMED,
        subject="Risultati della votazione AIM Voto",
        body="\n".join(lines) + "\n",
        confirmed_at=now,
    )
    EmailDelivery.objects.bulk_create(
        EmailDelivery(
            campaign=campaign,
            voter_id=voter_id,
            kind=MailKind.RESULTS,
            state=DeliveryState.QUEUED,
            available_at=now,
        )
        for voter_id in election.voters.values_list("pk", flat=True)
    )


@transaction.atomic
def create_preview(election_id, actor, kind, fields, *, voter_id=None):
    election = lock_election(election_id)
    now = database_now()
    if not is_open(election, now):
        raise ValidationError(
            "Gli invii si possono preparare durante una consultazione aperta."
        )
    section = fields.get("section", "")
    selected = recipients(election, kind)
    if kind == MailKind.RESEND:
        selected = [get_object_or_404(selected, pk=voter_id)]
    elif section:
        selected = selected.filter(section=section)
    campaign = ReminderCampaign.objects.create(
        election=election,
        kind=kind,
        author=actor,
        subject=fields["subject"],
        body=fields["body"],
        section=section,
    )
    deliveries = []
    for voter in selected:
        reason = exclusion(election, kind, voter, now)
        deliveries.append(
            EmailDelivery(
                campaign=campaign,
                voter=voter,
                kind=kind,
                state=DeliveryState.SKIPPED if reason else DeliveryState.PREVIEW,
                reason=reason,
            )
        )
    EmailDelivery.objects.bulk_create(deliveries)
    return campaign


@transaction.atomic
def confirm_campaign(election_id, campaign_id, actor):
    election = lock_election(election_id)
    campaign = get_object_or_404(election.campaigns, id=campaign_id, author=actor)
    if campaign.state != CampaignState.PREVIEW:
        return campaign
    now = database_now()
    if now >= campaign.created_at + PREVIEW_LIFETIME:
        raise ValidationError("L’anteprima è scaduta. Prepara un nuovo invio.")
    rows = list(campaign.deliveries.filter(state=DeliveryState.PREVIEW))
    if not rows:
        raise ValidationError("Nessun destinatario da confermare.")
    selected = recipients(election, campaign.kind, rows=campaign)
    by_id = {
        voter.pk: voter
        for voter in selected.filter(pk__in=[row.voter_id for row in rows])
    }
    for row in rows:
        voter = by_id[row.voter_id]
        reason = exclusion(election, campaign.kind, voter, now)
        if not reason and not active_link(voter):
            reason = DeliveryReason.INVALID_CREDENTIAL
        row.reason = reason
        row.state = DeliveryState.SKIPPED if reason else DeliveryState.QUEUED
        row.available_at = now
        row.finished_at = now if reason else None
    if not is_open(election, database_now()):
        for row in rows:
            if row.state == DeliveryState.QUEUED:
                row.state = DeliveryState.SKIPPED
                row.reason = DeliveryReason.CLOSED
                row.finished_at = now
    EmailDelivery.objects.bulk_update(
        rows, ("state", "reason", "available_at", "finished_at")
    )
    campaign.confirmed_at = now
    campaign.state = CampaignState.CONFIRMED
    campaign.save(update_fields=("confirmed_at", "state"))
    audit(
        "campagna_confermata",
        actor=actor,
        election=election,
        details={
            "campaign": str(campaign.pk),
            "kind": campaign.kind,
            "queued": sum(r.state == DeliveryState.QUEUED for r in rows),
        },
    )
    return campaign


@transaction.atomic
def cancel_campaign(election_id, campaign_id, actor):
    election = lock_election(election_id)
    campaign = get_object_or_404(election.campaigns, id=campaign_id)
    if campaign.state != CampaignState.CONFIRMED:
        return campaign
    now = timezone.now()
    cancelled = campaign.deliveries.filter(state=DeliveryState.QUEUED).update(
        state=DeliveryState.CANCELLED, reason=DeliveryReason.CANCELLED, finished_at=now
    )
    campaign.state = CampaignState.CANCELLED
    campaign.cancelled_at = now
    campaign.save(update_fields=("state", "cancelled_at"))
    audit(
        "campagna_annullata",
        actor=actor,
        election=election,
        details={"campaign": str(campaign.pk), "cancelled": cancelled},
    )
    return campaign


@transaction.atomic
def update_interval(election_id, actor, hours, reason):
    election = lock_election(election_id)
    previous = election.reminder_interval_hours
    if previous == hours:
        return
    election.reminder_interval_hours = hours
    election.save(update_fields=("reminder_interval_hours",))
    audit(
        "intervallo_solleciti_modificato",
        actor=actor,
        election=election,
        details={"previous_hours": previous, "hours": hours, "reason": reason},
    )
