import hashlib
import json
import secrets
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import (
    AuditEvent,
    Election,
    ElectionVoter,
    NamedBallot,
    Question,
    RateBucket,
    SubmissionReceipt,
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


def lock_election(election_id):
    return Election.objects.select_for_update().get(pk=election_id)


def is_open(election, now=None):
    now = timezone.now() if now is None else now
    return election.status == Election.Status.OPEN and now < election.closes_at


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


def open_locked(election, *, actor=None, now=None):
    now = database_now() if now is None else now
    if election.status not in {Election.Status.DRAFT, Election.Status.SCHEDULED}:
        raise ValidationError("La consultazione è già stata avviata.")
    if election.mode != Election.Mode.NAMED:
        raise ValidationError("Il voto segreto non è disponibile.")
    if now >= election.closes_at:
        raise ValidationError("La scadenza è già trascorsa.")
    if not election.questions.exists() or not election.voters.exists():
        raise ValidationError("Inserisci almeno un quesito e un elettore.")
    credentials = (
        VotingCredential(voter_id=voter_id, **credential_fields())
        for voter_id in election.voters.values_list("pk", flat=True).iterator()
    )
    VotingCredential.objects.bulk_create(credentials)
    election.opened_at = now
    election.status = Election.Status.OPEN
    election.save(update_fields=["opened_at", "status"])
    audit("apertura", actor=actor, election=election)


def close_locked(election, actor=None, now=None):
    now = database_now() if now is None else now
    if election.status != Election.Status.OPEN:
        raise ValidationError("Puoi chiudere soltanto una consultazione aperta.")
    election.ended_at = min(now, election.closes_at)
    election.status = Election.Status.CLOSED
    election.save(update_fields=["ended_at", "status"])
    audit("chiusura", actor=actor, election=election)


def advance_schedules():
    now = timezone.now()
    candidates = Election.objects.filter(
        status=Election.Status.SCHEDULED, opens_at__lte=now
    ) | Election.objects.filter(status=Election.Status.OPEN, closes_at__lte=now)
    for election_id in candidates.values_list("pk", flat=True):
        with transaction.atomic():
            election = lock_election(election_id)
            checked_at = database_now()
            if (
                election.status == Election.Status.SCHEDULED
                and election.opens_at <= checked_at
            ):
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
                else:
                    open_locked(election, now=checked_at)
            elif (
                election.status == Election.Status.OPEN
                and checked_at >= election.closes_at
            ):
                close_locked(election, now=checked_at)


def copy_link(election, voter, actor):
    if not is_open(election, database_now()):
        raise ValidationError("La votazione è chiusa o non ancora aperta.")
    url = personal_url(voter)
    audit("copia_link", actor=actor, election=election, voter=voter)
    return url


def rotate_link(election, voter, actor, reason):
    if not reason.strip() or not is_open(election, database_now()):
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
    for question_id, values in answers.items():
        question = allowed[question_id]
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
    return normalized


@transaction.atomic
def record_ballot(
    voter_id, election_id, credential_version, request_id, revision, answers
):
    election = lock_election(election_id)
    credential = VotingCredential.objects.filter(
        voter_id=voter_id, voter__election=election, version=credential_version
    ).first()
    if credential is None:
        raise ValidationError("Il link è stato revocato. Utilizza quello più recente.")
    ballot = NamedBallot.objects.filter(voter_id=voter_id).first()
    current_revision = ballot.revision if ballot else 0
    answers = validate_answers(ballot_questions(election), answers)
    request_id = UUID(str(request_id))
    fingerprint = salted_hmac(
        "aim-voto.submission",
        json.dumps([voter_id, str(request_id), revision, answers], sort_keys=True),
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

    # Punto di accettazione, dopo validazione e con il lock condiviso dalla chiusura.
    accepted_at = database_now()
    if not is_open(election, accepted_at):
        raise ValidationError("Votazione chiusa. La scheda non è stata registrata.")
    next_revision = revision + 1
    NamedBallot.objects.update_or_create(
        voter_id=voter_id,
        defaults={
            "answers": answers,
            "revision": next_revision,
            "submitted_at": accepted_at,
        },
    )
    receipt = SubmissionReceipt.objects.create(
        voter_id=voter_id,
        request_id=request_id,
        fingerprint=fingerprint,
        revision=next_revision,
        accepted_at=accepted_at,
    )
    return receipt, True


def electorate_snapshot(member):
    return {
        field: getattr(member, field)
        for field in (
            "external_id",
            "first_name",
            "last_name",
            "email",
            "phone",
            "section",
        )
    }


def set_electorate(election, members):
    ElectionVoter.objects.filter(election=election).delete()
    ElectionVoter.objects.bulk_create(
        ElectionVoter(election=election, member=member, **electorate_snapshot(member))
        for member in members
    )
