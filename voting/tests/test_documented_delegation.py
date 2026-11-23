from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from reportlab.pdfgen import canvas

from mailings.models import (
    DeliveryReason,
    DeliveryState,
    DispatchSchedule,
    EmailDelivery,
    MailKind,
)
from mailings.worker import process_next
from voting import proxy_access
from voting.models import Delegation, DelegationAttachment, NamedBallot

from .support import add_members, election_with_motion, open_election, redeem, staff


def attachment_pdf():
    output = BytesIO()
    document = canvas.Canvas(output)
    document.drawString(80, 750, "Domanda di delega dimostrativa")
    document.save()
    return output.getvalue()


def attachment_jpg():
    output = BytesIO()
    Image.new("RGB", (24, 24), "white").save(output, format="JPEG")
    return output.getvalue()


@override_settings(REQUIRE_ADMIN_MFA=False)
class DocumentedDelegationTests(TestCase):
    def setUp(self):
        self.actor = staff()
        self.admin = Client()
        self.admin.force_login(self.actor)
        self.election, self.question, self.options = election_with_motion(self.actor)
        self.election.allow_delegations = True
        self.election.save(update_fields=["allow_delegations"])
        self.delegate, *self.grantors = open_election(self.election, add_members(6))
        redeem(self.client, self.delegate)
        self.form = reverse("voting:received_delegation")

    def request(self, grantor, *, filename="domanda.pdf", content=None, **fields):
        pdf = attachment_pdf() if content is None else content
        return self.client.post(
            self.form,
            {
                "first_name": grantor.first_name,
                "last_name": grantor.last_name,
                "external_id": grantor.external_id,
                "file": SimpleUploadedFile(filename, pdf),
                **fields,
            },
        )

    def decision(self, item, action):
        return self.admin.post(
            reverse("voting:staff_delegation", args=[self.election.pk, item.pk, action])
        )

    def approved_request(self, grantor):
        self.assertEqual(self.request(grantor).status_code, 302)
        item = grantor.delegations_given.get()
        self.assertEqual(self.decision(item, "approve").status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.state, Delegation.State.APPROVED)
        return item

    def test_attachment_is_validated_and_visible_only_to_staff_for_its_consultation(
        self,
    ):
        grantor = self.grantors[0]
        self.assertContains(
            self.request(grantor, first_name="Nome errato"), "devono corrispondere"
        )
        self.assertContains(
            self.request(grantor, filename="domanda.jpg", content=b"fake"),
            "PDF o JPG valido",
        )
        self.assertFalse(Delegation.objects.exists())
        jpeg = attachment_jpg()
        self.assertEqual(
            self.request(grantor, filename="domanda.jpg", content=jpeg).status_code, 302
        )
        item = grantor.delegations_given.get()
        self.assertEqual(item.delegate, self.delegate)
        self.assertEqual(item.state, Delegation.State.PENDING)
        self.assertFalse(EmailDelivery.objects.filter(kind=MailKind.PROXY).exists())
        staff_url = reverse(
            "voting:delegation_attachment", args=[self.election.pk, item.pk]
        )
        self.assertEqual(self.client.get(staff_url).status_code, 302)
        another, *_ = election_with_motion(self.actor, title="Consultazione distinta")
        cross = reverse("voting:delegation_attachment", args=[another.pk, item.pk])
        self.assertEqual(self.admin.get(cross).status_code, 404)
        result = self.admin.get(staff_url)
        self.assertEqual(result["Content-Type"], "image/jpeg")
        self.assertIn("attachment", result["Content-Disposition"])
        self.assertEqual(b"".join(result.streaming_content), jpeg)

    def test_documented_pending_and_approved_requests_share_the_quota(self):
        original = Client()
        redeem(original, self.grantors[0])
        original.post(
            reverse("voting:assembly"),
            {"action": "delegate", "external_id": self.delegate.external_id},
        )
        existing = self.grantors[0].delegations_given.get()
        self.decision(existing, "approve")
        for index in (1, 2):
            self.assertEqual(self.request(self.grantors[index]).status_code, 302)
        self.assertContains(self.request(self.grantors[3]), "limite di 3 deleghe")
        pending = self.grantors[2].delegations_given.get()
        self.client.post(reverse("voting:voter_revoke", args=[pending.pk]))
        self.assertEqual(self.request(self.grantors[3]).status_code, 302)
        self.assertEqual(DelegationAttachment.objects.count(), 3)

    @patch("mailings.worker.send_local_message")
    def test_staff_approval_sends_one_scoped_link_per_request(self, send):
        first, second = (
            self.approved_request(grantor) for grantor in self.grantors[:2]
        )
        self.decision(first, "approve")
        self.assertEqual(EmailDelivery.objects.filter(kind=MailKind.PROXY).count(), 2)
        send.return_value = SimpleNamespace(state=DeliveryState.ACCEPTED, reason="")
        for _ in range(2):
            DispatchSchedule.objects.update(next_at=timezone.now())
            self.assertTrue(process_next())
        self.assertEqual(send.call_count, 2)
        notice = next(
            call.args[0]
            for call in send.call_args_list
            if call.args[0].delivery_id == first.notice.pk
        )
        self.assertEqual(notice.recipient, self.delegate.email)
        link = next(
            line.removeprefix("Il tuo link per questa delega: ")
            for line in notice.body.splitlines()
            if line.startswith("Il tuo link per questa delega: ")
        )
        token = urlsplit(link).fragment
        scoped = Client()
        access = scoped.post(reverse("voting:access"), {"token": token})
        scoped_url = reverse("voting:proxy_ballot", args=[first.pk])
        self.assertEqual(access.json()["ballot_url"], scoped_url)
        self.assertEqual(scoped.get(reverse("voting:ballot")).status_code, 403)
        self.assertEqual(
            scoped.get(reverse("voting:proxy_ballot", args=[second.pk])).status_code,
            404,
        )
        self.assertEqual(scoped.post(self.form).status_code, 403)
        preview = scoped.post(
            scoped_url, {"revision": 0, f"q{self.question.pk}": self.options[0].pk}
        )
        self.assertEqual(preview.status_code, 200)
        result = scoped.post(
            reverse("voting:submit"), {"payload": preview.context["payload"]}
        )
        self.assertContains(result, "Ricevuta di acquisizione")
        self.assertEqual(NamedBallot.objects.get().voter, first.grantor)
        self.assertEqual(NamedBallot.objects.get().cast_by, self.delegate)

    @patch("mailings.worker.send_local_message")
    def test_revocation_cancels_future_access_and_skips_queued_mail(self, send):
        item = self.approved_request(self.grantors[0])
        token = urlsplit(proxy_access.link(item, self.delegate.credential)).fragment
        scoped = Client()
        self.assertEqual(
            scoped.post(reverse("voting:access"), {"token": token}).status_code, 200
        )
        self.assertEqual(self.decision(item, "revoke").status_code, 302)
        self.assertEqual(
            scoped.post(reverse("voting:access"), {"token": token}).status_code, 404
        )
        self.assertEqual(
            scoped.get(reverse("voting:proxy_ballot", args=[item.pk])).status_code, 404
        )
        self.assertTrue(process_next())
        send.assert_not_called()
        item.notice.refresh_from_db()
        self.assertEqual(item.notice.state, DeliveryState.SKIPPED)
        self.assertEqual(item.notice.reason, DeliveryReason.INVALID_DELEGATION)
