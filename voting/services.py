import hashlib
import json
import secrets
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import ProtectedError, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables

from config.auth import require_superadmin

from . import quorum, secret_ballots
from .models import (
    MAX_ELECTORATE_SIZE,
    AuditEvent,
    Delegation,
    Election,
    NamedBallot,
    Question,
    RateBucket,
    SecretBallot,
    SubmissionReceipt,
    VoterPresence,
    VotingCredential,
)


def audit(action, *, actor=None, election=None, voter=None, details=None):
    AuditEvent.objects.create(
        action=action,
        actor=actor,
        election=election,
        voter=voter,
        details=details or {},
    )


def database_now():
    # PostgreSQL now() is pinned to transaction start, even while waiting for a lock.
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        return cursor.fetchone()[0]


def lock_election(election_id, *, allow_missing=False):
    elections = Election.objects.select_for_update().filter(pk=election_id)
    return elections.first() if allow_missing else elections.get()


def is_open(election, now=None):
    now = timezone.now() if now is None else now
    return election.status == Election.Status.OPEN and now < election.closes_at


def is_delegation_phase(election, now=None):
    now = timezone.now() if now is None else now
    return election.status == Election.Status.DELEGATIONS and now < election.closes_at


def delegations_open(election, now=None):
    return election.allow_delegations and (
        is_delegation_phase(election, now) or is_open(election, now)
    )


def links_available(election, now=None):
    return is_open(election, now) or is_delegation_phase(election, now)


def register_presence(election, voter_id, now, source):
    presence, created = VoterPresence.objects.get_or_create(
        voter_id=voter_id,
        defaults={"confirmed_at": now, "source": source},
    )
    if created:
        audit(
            "presenza_confermata",
            election=election,
            voter=presence.voter,
            details={"origine": source},
        )


def has_confirmed_presence(voter_id):
    return VoterPresence.objects.filter(
        voter_id=voter_id, source=VoterPresence.Source.EXPLICIT
    ).exists()


@transaction.atomic
def confirm_presence_from_ballot(voter_id, election_id, version, *, delegation_id=None):
    election = lock_election(election_id)
    now = database_now()
    if not is_open(election, now):
        raise ValidationError("La consultazione è chiusa.")
    if not VotingCredential.objects.filter(
        voter_id=voter_id, voter__election=election, version=version
    ).exists():
        raise ValidationError("Il link è stato revocato. Utilizza quello più recente.")
    if (
        delegation_id is not None
        and not Delegation.objects.filter(
            pk=delegation_id,
            delegate_id=voter_id,
            grantor__election=election,
            state=Delegation.State.APPROVED,
        ).exists()
    ):
        raise ValidationError("La delega non è più valida.")
    register_presence(election, voter_id, now, VoterPresence.Source.EXPLICIT)


def digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def link_cipher():
    return MultiFernet([Fernet(key) for key in settings.VOTING_LINK_KEYS])


def credential_fields():
    token = secrets.token_urlsafe(32)
    return {
        "digest": digest(token),
        "ciphertext": link_cipher().encrypt(token.encode("ascii")),
    }


def personal_url(voter):
    try:
        credential = VotingCredential.objects.get(voter_id=voter.pk)
        token = link_cipher().decrypt(bytes(credential.ciphertext)).decode("ascii")
    except (VotingCredential.DoesNotExist, InvalidToken, UnicodeDecodeError) as exc:
        raise ValidationError("Credenziale non disponibile.") from exc
    if not secrets.compare_digest(digest(token), credential.digest):
        raise ValidationError("Credenziale non valida.")
    return f"{settings.AIM_BASE_URL.rstrip('/')}{reverse('voting:access')}#{token}"


def prepare_opening(election, now):
    if now >= election.closes_at:
        raise ValidationError("La scadenza è già trascorsa.")
    electorate_size = election.voters.count()
    if not election.questions.exists() or not electorate_size:
        raise ValidationError("Inserisci almeno un quesito e un elettore.")
    if electorate_size > MAX_ELECTORATE_SIZE:
        raise ValidationError(
            f"La platea può contenere al massimo {MAX_ELECTORATE_SIZE} elettori."
        )
    quorum.validate_minimum_voters(election, electorate_size)
    if election.assembly_type:
        election.assembly_snapshot = quorum.configuration(election, electorate_size)
    credentials = (
        VotingCredential(voter_id=voter_id, **credential_fields())
        for voter_id in election.voters.filter(credential__isnull=True)
        .values_list("pk", flat=True)
        .iterator()
    )
    VotingCredential.objects.bulk_create(credentials)


def open_delegations_locked(election, *, actor, now):
    from mailings.campaigns import enqueue_opening_notice
    from mailings.models import MailKind

    require_superadmin(actor)
    if not election.allow_delegations or election.status not in {
        Election.Status.DRAFT,
        Election.Status.SCHEDULED,
    }:
        raise ValidationError(
            "L’apertura anticipata richiede una consultazione con deleghe abilitate, in bozza o programmata."
        )
    if election.opens_at and election.opens_at <= now:
        raise ValidationError(
            "L’orario previsto per il voto è già arrivato. Usa Apri per votazione."
        )
    prepare_opening(election, now)
    election.delegations_opened_at = now
    election.status = Election.Status.DELEGATIONS
    election.save(
        update_fields=["status", "delegations_opened_at", "assembly_snapshot"]
    )
    enqueue_opening_notice(election, actor, now, MailKind.EARLY)
    audit("apertura_per_deleghe", actor=actor, election=election)


def open_locked(election, *, actor=None, now=None):
    now = database_now() if now is None else now
    prior_phase = election.status == Election.Status.DELEGATIONS
    if election.status not in {
        Election.Status.DRAFT,
        Election.Status.SCHEDULED,
        Election.Status.DELEGATIONS,
    }:
        raise ValidationError("La consultazione è già stata avviata.")
    if prior_phase:
        if now >= election.closes_at:
            raise ValidationError("La scadenza è già trascorsa.")
    else:
        prepare_opening(election, now)
    election.opened_at = now
    election.status = Election.Status.OPEN
    election.save(update_fields=["opened_at", "status", "assembly_snapshot"])
    audit(
        "apertura",
        actor=actor,
        election=election,
        details={"configurazione_assembleare": election.assembly_snapshot["version"]}
        if election.assembly_snapshot
        else None,
    )
    if prior_phase:
        from mailings.campaigns import enqueue_opening_notice
        from mailings.models import MailKind

        enqueue_opening_notice(election, actor, now, MailKind.VOTE_OPEN)


def close_locked(election, actor=None, now=None):
    now = database_now() if now is None else now
    if election.status != Election.Status.OPEN:
        raise ValidationError("Puoi chiudere soltanto una consultazione aperta.")
    election.ended_at = min(now, election.closes_at)
    election.status = Election.Status.CLOSED
    election.save(update_fields=["ended_at", "status"])
    audit("chiusura", actor=actor, election=election)


@transaction.atomic
def delete_published_locked(election, *, actor):
    if election.status != Election.Status.TALLIED:
        raise ValidationError("Puoi eliminare solo una consultazione scrutinata.")
    election_id = election.pk
    try:
        # Remove records protected from incidental deletion of a voter or election.
        NamedBallot.objects.filter(voter__election=election).delete()
        SubmissionReceipt.objects.filter(voter__election=election).delete()
        SecretBallot.objects.filter(election=election).delete()
        election.delete()
    except ProtectedError as exc:
        raise ValidationError(
            "La consultazione ha dati collegati a un’altra consultazione. Contatta l’assistenza."
        ) from exc
    audit(
        "eliminazione_consultazione",
        actor=actor,
        details={"consultazione": election_id},
    )


def advance_schedules():
    now = timezone.now()
    waiting = (Election.Status.SCHEDULED, Election.Status.DELEGATIONS)
    candidates = Election.objects.filter(
        Q(status__in=waiting, opens_at__lte=now)
        | Q(
            status__in=(Election.Status.DELEGATIONS, Election.Status.OPEN),
            closes_at__lte=now,
        )
    )
    for election_id in candidates.values_list("pk", flat=True):
        with transaction.atomic():
            election = lock_election(election_id, allow_missing=True)
            if election is None:
                continue
            checked_at = database_now()
            if election.status in waiting:
                if checked_at >= election.closes_at:
                    election.status = Election.Status.CANCELLED
                    election.cancellation_reason = (
                        "La finestra programmata è trascorsa prima dell’apertura."
                    )
                    election.ended_at = checked_at
                    election.save(
                        update_fields=["status", "cancellation_reason", "ended_at"]
                    )
                    audit("programmazione_scaduta", election=election)
                elif election.opens_at and election.opens_at <= checked_at:
                    open_locked(election, now=checked_at)
            elif (
                election.status == Election.Status.OPEN
                and checked_at >= election.closes_at
            ):
                close_locked(election, now=checked_at)


def copy_link(election, voter, actor):
    require_superadmin(actor)
    now = database_now()
    if not links_available(election, now):
        raise ValidationError(
            "La consultazione deve essere aperta per deleghe o per votazione."
        )
    url = personal_url(voter)
    audit("copia_link", actor=actor, election=election, voter=voter)
    return url


def rotate_link(election, voter, actor, reason):
    require_superadmin(actor)
    now = database_now()
    if not reason.strip() or not links_available(election, now):
        raise ValidationError("Serve una motivazione e una consultazione aperta.")
    credential = VotingCredential.objects.get(voter=voter)
    for name, value in credential_fields().items():
        setattr(credential, name, value)
    credential.version += 1
    credential.save(update_fields=["digest", "ciphertext", "version"])
    audit(
        "revoca_link",
        actor=actor,
        election=election,
        voter=voter,
        details={"versione": credential.version, "motivo": reason},
    )


def admit_request(scope, identifier, limit, seconds):
    now = timezone.now()
    key = salted_hmac(
        "aim-voto.rate", f"{scope}:{identifier}", algorithm="sha256"
    ).hexdigest()
    with transaction.atomic():
        bucket, _ = RateBucket.objects.select_for_update().get_or_create(
            key=key, defaults={"since": now}
        )
        if (now - bucket.since).total_seconds() >= seconds:
            bucket.since = now
            bucket.hits = 0
        if bucket.hits >= limit:
            return False
        bucket.hits += 1
        bucket.save(update_fields=["since", "hits"])
    if secrets.randbelow(256) == 0:
        RateBucket.objects.filter(since__lt=now - timezone.timedelta(days=1)).delete()
    return True


def ballot_questions(election):
    return list(election.questions.prefetch_related("choices"))


def validate_answers(questions: list[Question], answers):
    if not isinstance(answers, dict):
        raise ValidationError("Scheda non valida.")
    allowed = {str(question.pk): question for question in questions}
    if set(answers) - set(allowed):
        raise ValidationError(
            "La scheda contiene un quesito estraneo alla consultazione."
        )
    normalized = {}
    for question_id, question in allowed.items():
        values = answers.get(question_id, [])
        options = {choice.pk for choice in question.choices.all()}
        if (
            not isinstance(values, list)
            or any(type(value) is not int or value not in options for value in values)
            or len(set(values)) != len(values)
            or len(values) > question.max_choices
        ):
            raise ValidationError(
                f"Preferenze non valide per il quesito «{question.text}»."
            )
        if values:
            normalized[question_id] = sorted(values)
        elif question.requires_answer:
            raise ValidationError(
                f"Seleziona una risposta per il quesito «{question.text}»."
            )
    return normalized


@transaction.atomic
@sensitive_variables("answers", "credential", "fingerprint_parts")
def record_ballot(
    voter_id,
    election_id,
    credential_version,
    request_id,
    revision,
    answers,
    *,
    delegation_id=None,
):
    election = lock_election(election_id)
    actor_id = voter_id
    credential = VotingCredential.objects.filter(
        voter_id=voter_id, voter__election=election, version=credential_version
    ).first()
    if credential is None:
        raise ValidationError("Il link è stato revocato. Utilizza quello più recente.")
    cast_by_id = None
    if delegation_id is not None:
        delegation = Delegation.objects.filter(
            pk=delegation_id,
            delegate_id=voter_id,
            grantor__election=election,
            state=Delegation.State.APPROVED,
        ).first()
        if not election.allow_delegations or delegation is None:
            raise ValidationError("La delega non è più valida.")
        cast_by_id, voter_id = voter_id, delegation.grantor_id
    elif Delegation.objects.filter(
        grantor_id=voter_id, state=Delegation.State.APPROVED
    ).exists():
        raise ValidationError("La posizione è assegnata al delegato autorizzato.")
    ballot = election.participation_records().filter(voter_id=voter_id).first()
    current_revision = ballot.revision if ballot else 0
    questions = ballot_questions(election)
    answers = validate_answers(questions, answers)
    request_id = UUID(str(request_id))
    fingerprint_parts = [voter_id, str(request_id), revision]
    if not election.is_secret:
        fingerprint_parts.append(answers)
    if cast_by_id is not None:
        fingerprint_parts.extend([cast_by_id, delegation_id])
    fingerprint = salted_hmac(
        "aim-voto.submission",
        json.dumps(fingerprint_parts, sort_keys=True),
        algorithm="sha256",
    ).hexdigest()
    receipt = SubmissionReceipt.objects.filter(
        voter_id=voter_id, request_id=request_id
    ).first()
    if receipt:
        if not secrets.compare_digest(receipt.fingerprint, fingerprint):
            raise ValidationError("L’identificativo appartiene a un altro invio.")
        return receipt, receipt.revision == current_revision
    if current_revision != revision:
        raise ValidationError(
            "La scheda è cambiata. Aprila e conferma di nuovo le tue scelte."
        )
    if ballot and not election.allow_replacement:
        raise ValidationError(
            "Hai già votato. Questa consultazione non consente sostituzioni."
        )
    if election.require_presence and not has_confirmed_presence(actor_id):
        raise ValidationError("Conferma la tua presenza prima di votare.")

    # Punto di accettazione, dopo validazione e con il lock condiviso dalla chiusura.
    accepted_at = database_now()
    if not is_open(election, accepted_at):
        raise ValidationError("Votazione chiusa. La scheda non è stata registrata.")
    next_revision = revision + 1
    if election.is_secret:
        secret_ballots.store(
            election,
            questions,
            answers,
            voter_id=voter_id,
            revision=next_revision,
            is_proxy=cast_by_id is not None,
        )
    else:
        NamedBallot.objects.update_or_create(
            voter_id=voter_id,
            defaults={
                "answers": answers,
                "revision": next_revision,
                "submitted_at": accepted_at,
                "cast_by_id": cast_by_id,
            },
        )
    receipt = SubmissionReceipt.objects.create(
        voter_id=voter_id,
        request_id=request_id,
        fingerprint=fingerprint,
        revision=next_revision,
        accepted_at=accepted_at,
        cast_by_id=cast_by_id if election.is_secret else None,
    )
    register_presence(election, actor_id, accepted_at, VoterPresence.Source.BALLOT)
    if cast_by_id is not None:
        audit(
            "scheda_per_delega",
            election=election,
            voter=delegation.grantor,
            details={
                "delega": delegation_id,
                "delegato": cast_by_id,
                "revisione": next_revision,
            },
        )
    return receipt, True
