import json
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.exceptions import ValidationError
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables

from .models import Election, SecretBallot

NONCE_SIZE = 12


def cipher(election_id):
    key = salted_hmac(
        "aim-voto.secret-ballot.encryption.v1", str(election_id), algorithm="sha256"
    ).digest()
    return AESGCM(key)


def encode(answers, is_proxy):
    return json.dumps([answers, is_proxy], separators=(",", ":")).encode("ascii")


@sensitive_variables()
def store(election, questions, answers, is_proxy):
    ballot = SecretBallot(election=election)
    # Use the same envelope length even for blank or partially completed ballots.
    all_choices = {
        str(question.pk): [choice.pk for choice in question.choices.all()]
        for question in questions
    }
    size = len(encode(all_choices, False))
    payload = encode(answers, is_proxy).ljust(size)
    nonce = token_bytes(NONCE_SIZE)
    ballot.ciphertext = nonce + cipher(election.pk).encrypt(
        nonce, payload, ballot.pk.bytes
    )
    ballot.save(force_insert=True)


@sensitive_variables()
def for_tally(election):
    if election.status not in (Election.Status.CLOSED, Election.Status.TALLIED):
        raise ValidationError("Lo scrutinio è disponibile dopo la chiusura.")
    decryptor = cipher(election.pk)
    ballots = []
    try:
        for ballot in SecretBallot.objects.filter(election=election).iterator():
            ciphertext = bytes(ballot.ciphertext)
            payload = decryptor.decrypt(
                ciphertext[:NONCE_SIZE], ciphertext[NONCE_SIZE:], ballot.pk.bytes
            )
            answers, is_proxy = json.loads(payload)
            ballots.append((answers, is_proxy))
    except (InvalidTag, ValueError) as exc:
        raise ValidationError(
            "Una scheda segreta non è leggibile. Scrutinio sospeso."
        ) from exc
    participation = election.participation_records()
    if (
        len(ballots) != participation.count()
        or sum(is_proxy for _, is_proxy in ballots)
        != participation.filter(cast_by__isnull=False).count()
    ):
        raise ValidationError(
            "Le schede segrete non coincidono con le partecipazioni registrate."
        )
    return ballots
