from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DataError
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
from voting.forms import ReceivedDelegationForm
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

    def upload_path(self, grantor):
        return reverse("voting:received_delegation_file", args=[grantor.pk])

    def request(self, grantor, *, filename="domanda.pdf", content=None):
        pdf = attachment_pdf() if content is None else content
        return self.client.post(
            self.upload_path(grantor),
            {"file": SimpleUploadedFile(filename, pdf)},
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

    def test_delegation_button_counts_approved_requests_and_highlights_when_ready(self):
        self.grantors[0].delegations_given.create(
            delegate=self.delegate,
            state=Delegation.State.APPROVED,
            requested_at=timezone.now(),
        )
        documented = self.approved_request(self.grantors[1])
        self.assertEqual(self.request(self.grantors[2]).status_code, 302)
        ballot = reverse("voting:ballot")
        delegations = reverse("voting:assembly")
        highlighted = f'<a class="button delegation-highlight" href="{delegations}">Deleghe (2)</a>'
        self.assertContains(self.client.get(ballot), highlighted, html=True)

        self.election.require_presence = True
        self.election.save(update_fields=["require_presence"])
        before = self.client.get(ballot)
        self.assertContains(before, "Deleghe (2)")
        self.assertNotContains(before, "delegation-highlight")
        self.assertRedirects(
            self.client.post(ballot, {"action": "confirm_presence"}), ballot
        )
        self.assertContains(self.client.get(ballot), highlighted, html=True)
        page = self.client.get(delegations)
        self.assertContains(page, "<h1>Deleghe</h1>", html=True)
        self.assertNotContains(page, "Confermo la mia presenza")
        self.assertContains(page, "Apri scheda in delega", count=2)

        proxy_ballot = reverse("voting:proxy_ballot", args=[documented.pk])
        self.assertContains(self.client.get(proxy_ballot), highlighted, html=True)
        scoped = Client()
        token = urlsplit(
            proxy_access.link(documented, self.delegate.credential)
        ).fragment
        scoped.post(reverse("voting:access"), {"token": token})
        self.assertNotContains(scoped.get(proxy_ballot), f'href="{delegations}"')

        self.decision(documented, "revoke")
        self.assertContains(self.client.get(ballot), "Deleghe (1)")

    def test_attachment_is_validated_and_visible_only_to_staff_for_its_consultation(
        self,
    ):
        grantor = self.grantors[0]
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

    def test_search_selects_a_grantor_in_the_same_electorate(self):
        grantor = self.grantors[0]
        grantor.first_name = "Àda"
        grantor.last_name = "Rossi"
        grantor.save(update_fields=["first_name", "last_name"])
        response = self.client.get(self.form, {"q": "ross ada", "format": "json"})
        (candidate,) = response.json()["candidates"]
        self.assertEqual(
            candidate,
            {
                "first_name": "Àda",
                "last_name": "Rossi",
                "external_id": grantor.external_id,
                "url": self.upload_path(grantor),
            },
        )
        self.assertContains(
            self.client.get(self.form, {"q": grantor.external_id}), "Àda Rossi"
        )
        self.assertEqual(
            Client().get(self.form, {"q": "ross", "format": "json"}).status_code, 403
        )
        self.assertContains(self.client.get(candidate["url"]), grantor.external_id)
        self.assertEqual(
            self.client.get(self.upload_path(self.delegate)).status_code, 404
        )
        other_election, *_ = election_with_motion(
            self.actor, title="Consultazione distinta"
        )
        (outsider,) = open_election(other_election, [grantor.member])
        self.assertEqual(self.request(outsider).status_code, 404)
        self.assertFalse(Delegation.objects.exists())

    def test_upload_read_error_is_reported_on_the_form(self):
        document = SimpleUploadedFile("domanda.pdf", attachment_pdf())
        form = ReceivedDelegationForm({}, {"file": document})
        with patch.object(document.file, "read", side_effect=OSError):
            self.assertFalse(form.is_valid())
        self.assertIn("Impossibile leggere il documento", str(form.errors["file"]))

    def test_attachment_save_error_does_not_leave_a_delegation(self):
        grantor = self.grantors[0]
        with patch(
            "voting.assembly.DelegationAttachment.objects.create", side_effect=DataError
        ):
            response = self.request(grantor)
        self.assertContains(response, "Salvataggio del documento non riuscito")
        self.assertFalse(grantor.delegations_given.exists())
        self.assertEqual(self.request(grantor).status_code, 302)

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
