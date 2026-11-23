from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mailings.models import DeliveryState, DispatchSchedule, MailKind
from mailings.transport import SmtpOutcome
from mailings.worker import process_next
from voting import services
from voting.models import Delegation, Election, ElectionDocument, VoterPresence

from .support import election_with_motion, redeem, staff


@override_settings(REQUIRE_ADMIN_MFA=False)
class DelegationOpeningTests(TestCase):
    def setUp(self):
        self.admin = staff(superadmin=True)
        self.client.force_login(self.admin)
        self.election, self.question, self.options = election_with_motion(self.admin)
        self.election.allow_delegations = True
        self.election.require_presence = True
        self.election.save()
        self.grantor, self.delegate, self.represented = [
            self.election.voters.create(
                first_name=f"Socio {index}",
                last_name="Fittizio",
                external_id=f"TEST-{index}",
                email=f"socio{index}@example.invalid",
            )
            for index in range(3)
        ]
        self.pdf = ElectionDocument.objects.create(
            election=self.election,
            name="Documento di prova.pdf",
            content=b"%PDF-1.4",
        )

    def transition(self, action, client=None):
        return (client or self.client).post(
            reverse("voting:transition", args=[self.election.pk, action]),
            {"confirm": "yes"},
        )

    def open_delegations(self):
        self.assertEqual(self.transition("apri-deleghe").status_code, 302)
        self.election.refresh_from_db()
        self.assertEqual(self.election.status, Election.Status.DELEGATIONS)
        self.assertIsNotNone(self.election.delegations_opened_at)
        self.assertIsNone(self.election.opened_at)

    def next_message(self, sender):
        DispatchSchedule.objects.filter(pk=1).update(next_at=timezone.now())
        self.assertTrue(process_next())
        return sender.call_args.args[0]

    @patch(
        "mailings.worker.send_local_message",
        return_value=SmtpOutcome(DeliveryState.ACCEPTED),
    )
    def test_early_access_and_manual_open_preserve_links_and_delegations(self, sender):
        regular_staff = get_user_model().objects.create(
            username="staff-limitato", is_staff=True
        )
        operator = Client()
        operator.force_login(regular_staff)
        self.assertEqual(self.transition("apri-deleghe", operator).status_code, 403)
        self.open_delegations()
        self.assertContains(self.transition("apri-deleghe"), "in bozza o programmata")
        self.assertEqual(self.election.campaigns.filter(kind=MailKind.EARLY).count(), 1)
        link = services.personal_url(self.delegate)
        grantor = Client()
        delegate = Client()
        self.assertEqual(redeem(grantor, self.grantor).status_code, 200)
        self.assertEqual(redeem(delegate, self.delegate).status_code, 200)
        ballot = reverse("voting:ballot")
        page = delegate.get(ballot)
        self.assertContains(page, "Aperta per deleghe")
        self.assertContains(page, self.pdf.name)
        self.assertNotContains(page, self.question.text)
        self.assertNotContains(page, "Conferma presenza")
        download = delegate.get(reverse("voting:voter_document", args=[self.pdf.pk]))
        self.assertEqual(b"".join(download.streaming_content), bytes(self.pdf.content))
        self.assertEqual(delegate.post(ballot, {"revision": 0}).status_code, 409)
        delegate.post(reverse("voting:assembly"), {"action": "presence"})
        self.assertFalse(VoterPresence.objects.exists())
        with self.assertRaises(ValidationError):
            services.record_ballot(
                self.delegate.pk,
                self.election.pk,
                self.delegate.credential.version,
                uuid4(),
                0,
                {},
            )
        self.assertFalse(self.election.participation_records().exists())

        consent = reverse("voting:confirm_direct_delegation", args=[self.delegate.pk])
        self.assertEqual(grantor.post(consent, {"confirm": "yes"}).status_code, 302)
        request_file = reverse(
            "voting:received_delegation_file", args=[self.represented.pk]
        )
        self.assertEqual(
            delegate.post(
                request_file, {"file": SimpleUploadedFile("Delega.pdf", b"%PDF-1.4")}
            ).status_code,
            302,
        )
        recorded = self.represented.delegations_given.get()
        self.assertContains(
            self.client.get(reverse("voting:manage_assembly", args=[self.election.pk])),
            self.represented.first_name,
        )
        self.assertEqual(
            self.client.post(
                reverse(
                    "voting:staff_delegation",
                    args=[self.election.pk, recorded.pk, "approve"],
                )
            ).status_code,
            302,
        )
        recorded.refresh_from_db()
        self.assertEqual(recorded.state, Delegation.State.APPROVED)
        self.assertNotContains(
            delegate.get(reverse("voting:assembly")), "ESERCITA DELEGA"
        )
        early = [self.next_message(sender) for _ in range(3)]
        email = next(
            message for message in early if message.recipient == self.delegate.email
        )
        self.assertIn(link, email.body)
        self.assertIn("gestire le deleghe e consultare i PDF", email.body)
        DispatchSchedule.objects.filter(pk=1).update(next_at=timezone.now())
        self.assertFalse(process_next())
        self.assertEqual(
            self.election.campaigns.filter(
                kind=MailKind.PROXY, deliveries__state=DeliveryState.QUEUED
            ).count(),
            2,
        )

        self.assertEqual(self.transition("apri", operator).status_code, 302)
        self.election.refresh_from_db()
        self.assertEqual(self.election.status, Election.Status.OPEN)
        self.assertEqual(services.personal_url(self.delegate), link)
        self.assertContains(delegate.get(ballot), "Conferma presenza e vai al voto")
        self.assertContains(grantor.get(ballot), "spetta al delegato autorizzato")
        opened = [self.next_message(sender) for _ in range(3)]
        email = next(
            message for message in opened if message.recipient == self.delegate.email
        )
        self.assertIn(link, email.body)
        self.assertIn("La votazione è aperta", email.body)
        self.assertIn("Il tuo link per questa delega", self.next_message(sender).body)

    def test_scheduled_opening_enqueues_the_same_link_once(self):
        self.election.opens_at = timezone.now() + timedelta(days=1)
        self.election.save(update_fields=["opens_at"])
        self.assertEqual(self.transition("programma").status_code, 302)
        self.open_delegations()
        credential = services.personal_url(self.delegate)
        services.advance_schedules()
        self.election.refresh_from_db()
        self.assertEqual(self.election.status, Election.Status.DELEGATIONS)
        self.assertFalse(
            self.election.campaigns.filter(kind=MailKind.VOTE_OPEN).exists()
        )
        Election.objects.filter(pk=self.election.pk).update(
            opens_at=timezone.now() - timedelta(seconds=1)
        )
        services.advance_schedules()
        services.advance_schedules()
        self.election.refresh_from_db()
        self.assertEqual(self.election.status, Election.Status.OPEN)
        self.assertEqual(services.personal_url(self.delegate), credential)
        notice = self.election.campaigns.get(kind=MailKind.VOTE_OPEN)
        self.assertEqual(
            notice.deliveries.filter(state=DeliveryState.QUEUED).count(), 3
        )
        self.assertFalse(
            self.election.campaigns.get(kind=MailKind.EARLY)
            .deliveries.filter(state=DeliveryState.QUEUED)
            .exists()
        )
