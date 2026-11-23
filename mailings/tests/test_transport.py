import smtplib
from email import message_from_bytes
from email.policy import SMTP
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from mailings.models import DeliveryReason, DeliveryState
from mailings.transport import PreparedMessage, SmtpOutcome, send_local_message


@override_settings(EMAIL_HOST="mailpit", DEFAULT_FROM_EMAIL="aim-voto@example.invalid")
class TransportTests(SimpleTestCase):
    def setUp(self):
        self.message = PreparedMessage(
            123,
            "<prova@example.invalid>",
            "destinatario@example.invalid",
            "Prova locale",
            "Test sintetico.",
        )

    def connect(self, smtp):
        client = smtp.return_value
        client.connect.return_value = (220, b"ready")
        client.mail.return_value = (250, b"ok")
        client.rcpt.return_value = (250, b"ok")
        client.data.return_value = (250, b"queued")
        return client

    @patch("mailings.transport.smtplib.SMTP")
    def test_success_is_acceptance_and_message_has_one_recipient(self, smtp):
        client = self.connect(smtp)
        self.assertEqual(
            send_local_message(self.message), SmtpOutcome(DeliveryState.ACCEPTED)
        )
        client.rcpt.assert_called_once_with(self.message.recipient)
        payload = message_from_bytes(client.data.call_args.args[0], policy=SMTP)
        self.assertEqual(str(payload["To"]), self.message.recipient)
        self.assertNotIn("cc", payload)
        self.assertNotIn("bcc", payload)
        self.assertEqual(payload.get_content().strip(), self.message.body)

    @patch("mailings.transport.smtplib.SMTP")
    def test_certain_temporary_refusal_can_be_retried(self, smtp):
        client = self.connect(smtp)
        client.rcpt.return_value = (451, b"try later")
        self.assertEqual(
            send_local_message(self.message),
            SmtpOutcome(DeliveryState.QUEUED, DeliveryReason.TRANSIENT),
        )
        client.data.assert_not_called()

    @patch("mailings.transport.smtplib.SMTP")
    def test_temporary_disconnect_during_data_is_uncertain(self, smtp):
        client = self.connect(smtp)
        client.data.side_effect = smtplib.SMTPServerDisconnected("unknown")
        self.assertEqual(
            send_local_message(self.message),
            SmtpOutcome(DeliveryState.UNCERTAIN, DeliveryReason.UNKNOWN),
        )

    @patch("mailings.transport.smtplib.SMTP")
    def test_permanent_rejection_is_not_retried(self, smtp):
        client = self.connect(smtp)
        client.rcpt.return_value = (550, b"unavailable")
        self.assertEqual(
            send_local_message(self.message),
            SmtpOutcome(DeliveryState.FAILED, DeliveryReason.PERMANENT),
        )
        client.data.assert_not_called()

    @override_settings(EMAIL_HOST="smtp.example.invalid")
    @patch("mailings.transport.smtplib.SMTP")
    def test_non_local_smtp_cannot_open_connection(self, smtp):
        with self.assertRaises(ImproperlyConfigured):
            send_local_message(self.message)
        smtp.assert_not_called()
