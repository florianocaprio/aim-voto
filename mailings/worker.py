from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from voting import proxy_access
from voting.models import Delegation, Election
from voting.services import database_now, is_delegation_phase, lock_election

from .campaigns import active_link, exclusion, recipients, render_body, window_exclusion
from .models import (
    CampaignState,
    DeliveryReason,
    DeliveryState,
    DispatchSchedule,
    EmailDelivery,
    MailKind,
)
from .transport import PreparedMessage, send_local_message


def recover_interrupted():
    now = timezone.now()
    timeout = timedelta(seconds=max(120, 8 * settings.EMAIL_TIMEOUT + 30))
    return EmailDelivery.objects.filter(
        state=DeliveryState.SENDING, started_at__lt=now - timeout
    ).update(
        state=DeliveryState.UNCERTAIN,
        reason=DeliveryReason.INTERRUPTED,
        finished_at=now,
    )


def _skip(delivery, reason, now):
    delivery.state = DeliveryState.SKIPPED
    delivery.reason = reason
    delivery.finished_at = now
    delivery.save(update_fields=("state", "reason", "finished_at"))


@transaction.atomic
def _claim(delivery_id, election_id):
    election = lock_election(election_id, allow_missing=True)
    if election is None:
        return None
    delivery = (
        EmailDelivery.objects.select_for_update()
        .select_related("campaign")
        .filter(
            pk=delivery_id,
            campaign__election_id=election_id,
            state=DeliveryState.QUEUED,
        )
        .first()
    )
    now = database_now()
    if delivery is None or delivery.available_at > now:
        return None
    if delivery.kind == MailKind.PROXY and is_delegation_phase(election, now):
        return None
    if delivery.campaign.state == CampaignState.CANCELLED:
        _skip(delivery, DeliveryReason.CANCELLED, now)
        return None
    audience = (
        election.voters
        if delivery.kind == MailKind.RESULTS
        else recipients(election, delivery.kind, rows=delivery.campaign)
    )
    voter = audience.get(pk=delivery.voter_id)
    reason = exclusion(election, delivery.kind, voter, now)
    if reason:
        _skip(delivery, reason, now)
        return None
    represented = None
    if delivery.kind == MailKind.RESULTS:
        link = ""
    elif delivery.kind == MailKind.PROXY:
        delegation = (
            Delegation.objects.select_related("grantor")
            .filter(
                pk=delivery.delegation_id,
                grantor__election=election,
                delegate=voter,
                state=Delegation.State.APPROVED,
            )
            .first()
        )
        if delegation is None or not election.allow_delegations:
            _skip(delivery, DeliveryReason.INVALID_DELEGATION, now)
            return None
        represented = delegation.grantor
        link = proxy_access.link(delegation, voter.credential)
    else:
        link = active_link(voter)
    if delivery.kind != MailKind.RESULTS and not link:
        _skip(delivery, DeliveryReason.INVALID_CREDENTIAL, now)
        return None
    schedule, _ = DispatchSchedule.objects.select_for_update().get_or_create(pk=1)
    now = database_now()
    reason = window_exclusion(election, delivery.kind, now)
    if reason:
        _skip(delivery, reason, now)
        return None
    if schedule.next_at > now:
        delivery.available_at = schedule.next_at
        delivery.save(update_fields=("available_at",))
        return None
    schedule.next_at = now + timedelta(minutes=1) / settings.MAIL_SEND_PER_MINUTE
    schedule.save(update_fields=("next_at",))
    delivery.state = DeliveryState.SENDING
    delivery.reason = ""
    delivery.started_at = now
    delivery.attempt_count += 1
    delivery.save(update_fields=("state", "reason", "started_at", "attempt_count"))
    return PreparedMessage(
        delivery.pk,
        delivery.smtp_message_id,
        voter.email,
        delivery.campaign.subject,
        render_body(delivery.campaign, voter, link, represented=represented),
    )


@transaction.atomic
def _finish(prepared, election_id, outcome):
    if lock_election(election_id, allow_missing=True) is None:
        return
    delivery = (
        EmailDelivery.objects.select_for_update()
        .select_related("campaign")
        .filter(pk=prepared.delivery_id, state=DeliveryState.SENDING)
        .first()
    )
    if delivery is None:
        return
    now = timezone.now()
    delivery.state = outcome.state
    delivery.reason = outcome.reason
    delivery.finished_at = now
    if outcome.state == DeliveryState.QUEUED:
        if delivery.campaign.state == CampaignState.CANCELLED:
            delivery.state = DeliveryState.CANCELLED
            delivery.reason = DeliveryReason.CANCELLED
        elif delivery.attempt_count >= settings.MAIL_MAX_ATTEMPTS:
            delivery.state = DeliveryState.FAILED
        else:
            delay_minutes = min(60, 2 ** min(delivery.attempt_count - 1, 6))
            delivery.available_at = now + timedelta(minutes=delay_minutes)
            delivery.finished_at = None
    delivery.save(update_fields=("state", "reason", "finished_at", "available_at"))


def process_next():
    now = timezone.now()
    candidate = (
        EmailDelivery.objects.filter(state=DeliveryState.QUEUED, available_at__lte=now)
        .exclude(
            kind=MailKind.PROXY,
            campaign__election__status=Election.Status.DELEGATIONS,
            campaign__election__closes_at__gt=now,
        )
        .order_by("available_at", "pk")
        .values_list("pk", "campaign__election_id")
        .first()
    )
    if candidate is None:
        return False
    if DispatchSchedule.objects.filter(pk=1, next_at__gt=timezone.now()).exists():
        return False
    delivery_id, election_id = candidate
    prepared = _claim(delivery_id, election_id)
    if prepared is not None:
        # A completed claim is durable before making the SMTP connection.
        outcome = send_local_message(prepared)
        _finish(prepared, election_id, outcome)
    return True
