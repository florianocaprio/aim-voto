from django.core.exceptions import ValidationError
from django.db.models import Q

from members.importing import cell_text

from .models import Delegation, DelegationAttachment, NamedBallot
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


def reserved_slots(election, delegate_id):
    return Delegation.objects.filter(
        grantor__election=election,
        delegate_id=delegate_id,
    ).filter(
        Q(state=Delegation.State.APPROVED)
        | Q(state=Delegation.State.PENDING, attachment__isnull=False)
    )


def request_received_delegation(election, delegate, fields):
    now = require_delegations_open(election)
    grantor = (
        election.voters.filter(external_id=cell_text(fields["external_id"]))
        .exclude(pk=delegate.pk)
        .first()
    )
    if grantor is None or (
        grantor.first_name.casefold(),
        grantor.last_name.casefold(),
    ) != (
        cell_text(fields["first_name"]).casefold(),
        cell_text(fields["last_name"]).casefold(),
    ):
        raise ValidationError(
            "Nome, cognome e matricola devono corrispondere a un altro avente diritto."
        )
    if grantor.delegations_given.filter(state__in=ACTIVE).exists():
        raise ValidationError(
            "Per questo delegante esiste già una richiesta in verifica o approvata."
        )
    if NamedBallot.objects.filter(voter=grantor).exists():
        raise ValidationError(
            "La posizione del delegante ha già una scheda registrata."
        )
    limit = delegation_limit(election.voters.count())
    if reserved_slots(election, delegate.pk).count() >= limit:
        raise ValidationError(
            f"Hai raggiunto il limite di {limit} deleghe presentate o approvate."
        )
    delegation = Delegation.objects.create(
        grantor=grantor, delegate=delegate, requested_at=now
    )
    filename, mime_type, content = fields["file"]
    DelegationAttachment.objects.create(
        delegation=delegation,
        filename=filename,
        mime_type=mime_type,
        content=content,
    )
    audit(
        "delega_presentata_dal_delegato",
        election=election,
        voter=grantor,
        details={"delega": delegation.pk, "delegato": delegate.pk},
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
            if (
                reserved_slots(election, delegation.delegate_id)
                .exclude(pk=delegation.pk)
                .count()
                >= limit
            ):
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
    if (
        action == "approve"
        and DelegationAttachment.objects.filter(delegation=delegation).exists()
    ):
        from mailings.campaigns import enqueue_proxy_notice

        enqueue_proxy_notice(election, delegation, actor)
