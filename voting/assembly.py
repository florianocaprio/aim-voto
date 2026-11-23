from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import Delegation, NamedBallot
from .services import audit, database_now, is_open

ACTIVE = (Delegation.State.PENDING, Delegation.State.APPROVED)


def delegation_limit(eligible):
    return 3 if eligible <= 500 else 5


def require_delegations_open(election):
    now = database_now()
    if not election.allow_delegations or not is_open(election, now):
        raise ValidationError(
            "Le deleghe sono disponibili durante una consultazione aperta che le ammette."
        )
    return now


def request_delegation(election, grantor, external_id):
    # The caller holds the election lock, also used by staff verification and ballot submission.
    now = require_delegations_open(election)
    delegate = (
        election.voters.filter(external_id=external_id).exclude(pk=grantor.pk).first()
    )
    if delegate is None:
        raise ValidationError(
            "Indica l’ID di un altro avente diritto di questa consultazione."
        )
    if NamedBallot.objects.filter(voter=grantor).exists():
        raise ValidationError("La tua posizione ha già una scheda registrata.")
    if grantor.delegations_given.filter(state__in=ACTIVE).exists():
        raise ValidationError("Hai già una delega in verifica o approvata.")
    delegation = Delegation.objects.create(
        grantor=grantor, delegate=delegate, requested_at=now
    )
    audit(
        "delega_richiesta",
        election=election,
        voter=grantor,
        details={"delega": delegation.pk},
    )
    return delegation


def decide_delegation(election, delegation, action, *, actor=None):
    require_delegations_open(election)
    status = Delegation.State
    if (
        action in {"approve", "reject"}
        and delegation.state == status.PENDING
        and actor is not None
    ):
        if action == "approve":
            if NamedBallot.objects.filter(voter_id=delegation.grantor_id).exists():
                raise ValidationError(
                    "La posizione del delegante ha già una scheda registrata."
                )
            approved = Delegation.objects.filter(
                state=status.APPROVED,
                grantor__election=election,
                delegate__election=election,
            )
            if approved.filter(
                Q(delegate_id=delegation.grantor_id)
                | Q(grantor_id=delegation.delegate_id)
            ).exists():
                raise ValidationError(
                    "Una delega approvata impedisce di creare una catena di deleghe."
                )
            limit = delegation_limit(election.voters.count())
            if approved.filter(delegate_id=delegation.delegate_id).count() >= limit:
                raise ValidationError(
                    f"Il delegato ha raggiunto il limite di {limit} deleghe."
                )
            delegation.state = status.APPROVED
        else:
            delegation.state = status.REJECTED
    elif action == "revoke" and delegation.state in ACTIVE:
        if NamedBallot.objects.filter(
            voter_id=delegation.grantor_id, cast_by__isnull=False
        ).exists():
            raise ValidationError(
                "Il delegato ha già registrato una scheda. La delega resta valida."
            )
        delegation.state = status.REVOKED
    else:
        raise ValidationError(
            "L’operazione non è disponibile nello stato attuale della delega."
        )
    delegation.save(update_fields=["state"])
    audit(
        "delega_" + delegation.state,
        actor=actor,
        election=election,
        voter=delegation.grantor,
        details={"delega": delegation.pk},
    )
