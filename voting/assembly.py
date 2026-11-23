import unicodedata

from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import Delegation, DelegationAttachment
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
    if election.participation_records().filter(voter=grantor).exists():
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


def request_received_delegation(election, delegate, grantor, document):
    now = require_delegations_open(election)
    if (
        grantor.election_id != election.pk
        or delegate.election_id != election.pk
        or grantor.pk == delegate.pk
    ):
        raise ValidationError(
            "Seleziona un altro avente diritto a questa consultazione."
        )
    if grantor.delegations_given.filter(state__in=ACTIVE).exists():
        raise ValidationError(
            "Per questo delegante esiste già una richiesta in verifica o approvata."
        )
    if election.participation_records().filter(voter=grantor).exists():
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
    filename, mime_type, content = document
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


def validate_delegation_approval(election, grantor_id, delegate_id, existing_pk=None):
    if election.participation_records().filter(voter_id=grantor_id).exists():
        raise ValidationError(
            "La posizione del delegante ha già una scheda registrata."
        )
    approved = Delegation.objects.filter(
        state=Delegation.State.APPROVED,
        grantor__election=election,
        delegate__election=election,
    )
    if approved.filter(Q(delegate_id=grantor_id) | Q(grantor_id=delegate_id)).exists():
        raise ValidationError(
            "Una delega approvata impedisce di creare una catena di deleghe."
        )
    limit = delegation_limit(election.voters.count())
    if reserved_slots(election, delegate_id).exclude(pk=existing_pk).count() >= limit:
        raise ValidationError(f"Il delegato ha raggiunto il limite di {limit} deleghe.")


def assign_direct_delegation(election, grantor, delegate):
    from mailings.campaigns import enqueue_proxy_notice

    now = require_delegations_open(election)
    if (
        not election.require_presence
        or grantor.election_id != election.pk
        or delegate.election_id != election.pk
        or grantor.pk == delegate.pk
    ):
        raise ValidationError("La delega diretta non è disponibile per questa scelta.")
    active = grantor.delegations_given.filter(state__in=ACTIVE).first()
    if active:
        if (
            active.state == Delegation.State.APPROVED
            and active.delegate_id == delegate.pk
        ):
            return active
        raise ValidationError("Hai già una delega in verifica o approvata.")
    validate_delegation_approval(election, grantor.pk, delegate.pk)
    delegation = Delegation.objects.create(
        grantor=grantor,
        delegate=delegate,
        requested_at=now,
        state=Delegation.State.APPROVED,
    )
    audit(
        "delega_diretta_conferita",
        election=election,
        voter=grantor,
        details={"delega": delegation.pk, "delegato": delegate.pk},
    )
    enqueue_proxy_notice(election, delegation, actor=None)
    return delegation


def search_key(value):
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in folded if char.isalnum())


def search_delegates(voter, query):
    terms = [key for term in query.split() if (key := search_key(term))]
    if sum(map(len, terms)) < 2:
        return []
    candidates = voter.election.voters.exclude(pk=voter.pk).values(
        "pk", "first_name", "last_name", "external_id"
    )
    matches = []
    for candidate in candidates:
        fields = (
            search_key(candidate["first_name"]),
            search_key(candidate["last_name"]),
            search_key(candidate["external_id"]),
        )
        if not all(any(term in field for field in fields) for term in terms):
            continue
        prefix_penalty = sum(
            not any(field.startswith(term) for field in fields) for term in terms
        )
        matches.append((prefix_penalty, candidate))
    matches.sort(key=lambda item: item[0])
    return [candidate for _, candidate in matches[:10]]


def decide_delegation(election, delegation, action, *, actor=None):
    require_delegations_open(election)
    status = Delegation.State
    if (
        action in {"approve", "reject"}
        and delegation.state == status.PENDING
        and actor is not None
    ):
        if action == "approve":
            validate_delegation_approval(
                election, delegation.grantor_id, delegation.delegate_id, delegation.pk
            )
            delegation.state = status.APPROVED
        else:
            delegation.state = status.REJECTED
    elif action == "revoke" and delegation.state in ACTIVE:
        if (
            election.participation_records()
            .filter(voter_id=delegation.grantor_id, cast_by__isnull=False)
            .exists()
        ):
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
