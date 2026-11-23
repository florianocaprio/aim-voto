import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .campaigns import usable_address
from .models import DeliveryReason, DeliveryState


@dataclass(frozen=True)
class PreparedMessage:
    delivery_id: int
    message_id: str
    recipient: str = field(repr=False)
    subject: str = field(repr=False)
    body: str = field(repr=False)


@dataclass(frozen=True)
class SmtpOutcome:
    state: str
    reason: str = ""


def check_local_transport():
    if settings.EMAIL_HOST not in ("mailpit", "127.0.0.1", "::1", "localhost"):
        raise ImproperlyConfigured(
            "AIM Voto consente l’invio SMTP esclusivamente al Mailpit di sviluppo."
        )
    if not usable_address(settings.DEFAULT_FROM_EMAIL):
        raise ImproperlyConfigured(
            "DEFAULT_FROM_EMAIL deve essere un indirizzo sintetico example.invalid."
        )


def _reply_outcome(code, *, message_transmitted=False):
    if message_transmitted and code == 250:
        return SmtpOutcome(DeliveryState.ACCEPTED)
    if 400 <= code < 500:
        return SmtpOutcome(DeliveryState.QUEUED, DeliveryReason.TRANSIENT)
    if 500 <= code < 600:
        return SmtpOutcome(DeliveryState.FAILED, DeliveryReason.PERMANENT)
    if message_transmitted:
        return SmtpOutcome(DeliveryState.UNCERTAIN, DeliveryReason.UNKNOWN)
    return SmtpOutcome(DeliveryState.FAILED, DeliveryReason.PERMANENT)


def send_local_message(prepared):
    check_local_transport()
    if not usable_address(prepared.recipient):
        return SmtpOutcome(DeliveryState.FAILED, DeliveryReason.INVALID_EMAIL)
    message = EmailMessage(policy=SMTP)
    message["From"] = settings.DEFAULT_FROM_EMAIL
    message["To"] = prepared.recipient
    message["Subject"] = prepared.subject
    message["Message-ID"] = prepared.message_id
    message.set_content(prepared.body, cte="quoted-printable")

    client = smtplib.SMTP(timeout=settings.EMAIL_TIMEOUT)
    sending_data = False
    try:
        code, _ = client.connect(settings.EMAIL_HOST, settings.EMAIL_PORT)
        if code != 220:
            return _reply_outcome(code)
        client.ehlo_or_helo_if_needed()
        code, _ = client.mail(settings.DEFAULT_FROM_EMAIL)
        if not 200 <= code < 300:
            return _reply_outcome(code)
        code, _ = client.rcpt(prepared.recipient)
        if not 200 <= code < 300:
            return _reply_outcome(code)
        sending_data = True
        code, _ = client.data(message.as_bytes())
        return _reply_outcome(code, message_transmitted=True)
    except smtplib.SMTPResponseException as error:
        # The server explicitly rejected this step. Keep only a generic reason.
        return _reply_outcome(error.smtp_code)
    except OSError:
        if sending_data:
            return SmtpOutcome(DeliveryState.UNCERTAIN, DeliveryReason.UNKNOWN)
        return SmtpOutcome(DeliveryState.QUEUED, DeliveryReason.TRANSIENT)
    finally:
        client.close()
