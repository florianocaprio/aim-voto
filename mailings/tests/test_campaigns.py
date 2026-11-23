from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mailings.campaigns import cancel_campaign, confirm_campaign, create_preview
from mailings.models import (
    DeliveryReason,
    DeliveryState,
    DispatchSchedule,
    MailKind,
    ReminderCampaign,
)
from mailings.transport import SmtpOutcome
from mailings.worker import _claim, process_next, recover_interrupted
from members.models import Member
from voting.models import (
    AuditEvent,
    Election,
    ElectionVoter,
    NamedBallot,
    VotingCredential,
)
from voting.services import credential_fields, electorate_snapshot, personal_url

MESSAGE = {
    "subject": "Promemoria di prova",
    "body": "Messaggio sintetico della campagna.",
}


def create_election(actor):
    return Election.objects.create(
        title="Consultazione sintetica",
        created_by=actor,
        status=Election.Status.OPEN,
        closes_at=timezone.now() + timedelta(days=2),
    )


def create_voter(election, serial, section="Nord", address=None):
    member = Member.objects.create(
        external_id=f"TEST-{serial}",
        first_name=f"Nome {serial}",
        last_name="Esempio",
        email=address or f"socio{serial}@example.invalid",
        section=section,
    )
    voter = ElectionVoter.objects.create(
        election=election, member=member, **electorate_snapshot(member)
    )
    VotingCredential.objects.create(voter=voter, **credential_fields())
    return voter


@override_settings(REQUIRE_ADMIN_MFA=False)
class CampaignTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create(
            username="operatore-demo", is_staff=True
        )
        self.election = create_election(self.admin)
        self.client.force_login(self.admin)

    def preview(self, kind=MailKind.REMINDER, voter_id=None, **fields):
        return create_preview(
            self.election.pk, self.admin, kind, MESSAGE | fields, voter_id=voter_id
        )

    def confirm(self, campaign):
        return confirm_campaign(self.election.pk, campaign.pk, self.admin)

    def assert_delivery(self, campaign, voter, state, reason=""):
        row = campaign.deliveries.get(voter=voter)
        self.assertEqual((row.state, row.reason), (state, reason))
        return row

    def clear_throttle(self):
        DispatchSchedule.objects.update(next_at=timezone.now() - timedelta(seconds=1))

    def test_reminder_preview_excludes_blank_abstention_and_other_section(self):
        blank, abstention, eligible = [
            create_voter(self.election, number) for number in range(1, 4)
        ]
        create_voter(self.election, 4, section="Sud")
        create_voter(create_election(self.admin), 5)
        NamedBallot.objects.create(voter=blank)
        NamedBallot.objects.create(voter=abstention, answers={"1": [3]})

        campaign = self.preview(section="Nord")
        self.assertEqual(campaign.deliveries.count(), 3)
        self.assert_delivery(
            campaign, blank, DeliveryState.SKIPPED, DeliveryReason.VOTED
        )
        self.assert_delivery(
            campaign, abstention, DeliveryState.SKIPPED, DeliveryReason.VOTED
        )
        self.assert_delivery(campaign, eligible, DeliveryState.PREVIEW)
        response = self.client.get(
            reverse("mailings:campaign", args=(self.election.pk, campaign.pk))
        )
        self.assertContains(response, "Invia sollecito a 1 socio")
        self.assertFalse(personal_url(eligible).encode() in response.content)

        self.confirm(campaign)
        self.confirm(campaign)
        self.assertEqual(
            campaign.deliveries.filter(state=DeliveryState.QUEUED).count(), 1
        )
        self.assertEqual(
            AuditEvent.objects.filter(action="campagna_confermata").count(), 1
        )

    def test_confirmation_preserves_target_selection_and_rechecks_vote(self):
        voter = create_voter(self.election, 1)
        campaign = self.preview()
        NamedBallot.objects.create(voter=voter)
        other = create_voter(self.election, 2)
        self.confirm(campaign)
        self.assert_delivery(
            campaign, voter, DeliveryState.SKIPPED, DeliveryReason.VOTED
        )
        self.assertFalse(campaign.deliveries.filter(voter=other).exists())

    def test_worker_rechecks_voted_after_confirmation(self):
        voter = create_voter(self.election, 1)
        campaign = self.preview()
        self.confirm(campaign)
        NamedBallot.objects.create(voter=voter)
        with patch("mailings.worker.send_local_message") as send:
            self.assertTrue(process_next())
            send.assert_not_called()
        self.assert_delivery(
            campaign, voter, DeliveryState.SKIPPED, DeliveryReason.VOTED
        )

    def test_worker_rechecks_deadline_when_status_has_not_advanced(self):
        voter = create_voter(self.election, 1)
        campaign = self.preview()
        self.confirm(campaign)
        Election.objects.filter(pk=self.election.pk).update(closes_at=timezone.now())
        with patch("mailings.worker.send_local_message") as send:
            self.assertTrue(process_next())
            send.assert_not_called()
        self.assert_delivery(
            campaign, voter, DeliveryState.SKIPPED, DeliveryReason.CLOSED
        )

    def test_worker_prepares_two_individual_messages_for_shared_email_with_active_links(
        self,
    ):
        shared_email = "condivisa@example.invalid"
        first = create_voter(self.election, 1, address=shared_email)
        second = create_voter(self.election, 2, address=shared_email)
        old = personal_url(first)
        campaign = self.preview(MailKind.INVITE)
        self.confirm(campaign)
        VotingCredential.objects.filter(voter=first).update(
            version=2, **credential_fields()
        )
        first_link, second_link = personal_url(first), personal_url(second)

        with patch(
            "mailings.worker.send_local_message",
            return_value=SmtpOutcome(DeliveryState.ACCEPTED),
        ) as send:
            process_next()
            self.clear_throttle()
            process_next()
        first_message, second_message = [call.args[0] for call in send.call_args_list]
        self.assertEqual(
            (first_message.recipient, second_message.recipient),
            (shared_email, shared_email),
        )
        self.assertTrue(
            first_link in first_message.body, "Link attivo assente dal messaggio."
        )
        self.assertFalse(
            old in first_message.body, "Link estraneo o revocato nel messaggio."
        )
        self.assertFalse(
            second_link in first_message.body, "Link estraneo o revocato nel messaggio."
        )
        self.assertTrue(
            second_link in second_message.body, "Link attivo assente dal messaggio."
        )
        self.assertFalse(
            first_link in second_message.body, "Link estraneo o revocato nel messaggio."
        )
        self.assert_delivery(campaign, first, DeliveryState.ACCEPTED)
        self.assert_delivery(campaign, second, DeliveryState.ACCEPTED)

    def test_corrupt_credential_and_address_incompatible_with_transport_are_excluded(
        self,
    ):
        voter = create_voter(self.election, 1)
        invalid = create_voter(self.election, 2, address="sòcio@example.invalid")
        campaign = self.preview()
        VotingCredential.objects.filter(voter=voter).update(ciphertext=b"invalid")
        self.confirm(campaign)
        self.assert_delivery(
            campaign, voter, DeliveryState.SKIPPED, DeliveryReason.INVALID_CREDENTIAL
        )
        self.assert_delivery(
            campaign, invalid, DeliveryState.SKIPPED, DeliveryReason.INVALID_EMAIL
        )

    def test_pending_and_recent_reminder_block_others_but_not_individual_reinvio(self):
        voter = create_voter(self.election, 1)
        first, parallel = self.preview(), self.preview()
        self.confirm(first)
        self.confirm(parallel)
        self.assert_delivery(
            parallel, voter, DeliveryState.SKIPPED, DeliveryReason.PENDING
        )
        with patch(
            "mailings.worker.send_local_message",
            return_value=SmtpOutcome(DeliveryState.ACCEPTED),
        ):
            process_next()
        recent = self.preview()
        self.assert_delivery(
            recent, voter, DeliveryState.SKIPPED, DeliveryReason.FREQUENCY
        )
        resend = self.preview(MailKind.RESEND, voter_id=voter.pk)
        self.confirm(resend)
        self.assert_delivery(resend, voter, DeliveryState.QUEUED)

    def test_retry_for_certain_temporary_error_then_uncertain_is_final(self):
        voter = create_voter(self.election, 1)
        campaign = self.preview()
        self.confirm(campaign)
        transient = SmtpOutcome(DeliveryState.QUEUED, DeliveryReason.TRANSIENT)
        ambiguous = SmtpOutcome(DeliveryState.UNCERTAIN, DeliveryReason.UNKNOWN)
        with patch(
            "mailings.worker.send_local_message", side_effect=[transient, ambiguous]
        ) as send:
            process_next()
            row = self.assert_delivery(
                campaign, voter, DeliveryState.QUEUED, DeliveryReason.TRANSIENT
            )
            self.assertEqual(row.attempt_count, 1)
            self.assertGreater(row.available_at, timezone.now())
            self.assertFalse(process_next())
            campaign.deliveries.update(available_at=timezone.now())
            self.clear_throttle()
            process_next()
            self.assertFalse(process_next())
            self.assertEqual(send.call_count, 2)
        self.assert_delivery(
            campaign, voter, DeliveryState.UNCERTAIN, DeliveryReason.UNKNOWN
        )

    def test_cancellation_preserves_accepted_and_only_cancels_queued(self):
        first, second = [create_voter(self.election, number) for number in (1, 2)]
        campaign = self.preview()
        self.confirm(campaign)
        with patch(
            "mailings.worker.send_local_message",
            return_value=SmtpOutcome(DeliveryState.ACCEPTED),
        ) as send:
            process_next()
            cancel_campaign(self.election.pk, campaign.pk, self.admin)
            self.assertFalse(process_next())
            send.assert_called_once()
        self.assert_delivery(campaign, first, DeliveryState.ACCEPTED)
        self.assert_delivery(
            campaign, second, DeliveryState.CANCELLED, DeliveryReason.CANCELLED
        )
        self.assertEqual(
            AuditEvent.objects.filter(
                action="campagna_annullata", actor=self.admin
            ).count(),
            1,
        )

    def test_interrupted_dispatch_becomes_uncertain_and_stays_out_of_queue(self):
        voter = create_voter(self.election, 1)
        campaign = self.preview()
        self.confirm(campaign)
        delivery = campaign.deliveries.get()
        self.assertIsNotNone(_claim(delivery.pk, self.election.pk))
        campaign.deliveries.update(started_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(recover_interrupted(), 1)
        self.assert_delivery(
            campaign, voter, DeliveryState.UNCERTAIN, DeliveryReason.INTERRUPTED
        )
        self.assertFalse(process_next())

    def test_post_routes_require_staff_and_scope_to_the_election(self):
        voter = create_voter(self.election, 1)
        campaign = self.preview()
        another = create_election(self.admin)
        crossed = reverse("mailings:confirm", args=(another.pk, campaign.pk))
        bad_resend = reverse("mailings:resend", args=(another.pk, voter.pk))
        self.assertEqual(self.client.post(crossed).status_code, 404)
        self.assertEqual(self.client.get(bad_resend).status_code, 404)
        regular = get_user_model().objects.create(username="elettore-puro")
        self.client.force_login(regular)
        correct = reverse("mailings:confirm", args=(self.election.pk, campaign.pk))
        self.assertEqual(self.client.post(correct).status_code, 403)
        self.assertEqual(ReminderCampaign.objects.get(pk=campaign.pk).state, "preview")

    def test_reminder_interval_change_has_audit_requirements(self):
        endpoint = reverse("mailings:interval", args=(self.election.pk,))
        self.client.post(endpoint, {"hours": 12, "reason": ""})
        self.election.refresh_from_db()
        self.assertEqual(self.election.reminder_interval_hours, 24)
        self.client.post(endpoint, {"hours": 12, "reason": "Finestra della prova"})
        self.election.refresh_from_db()
        self.assertEqual(self.election.reminder_interval_hours, 12)
        event = AuditEvent.objects.get(
            action="intervallo_solleciti_modificato", actor=self.admin
        )
        self.assertEqual(event.details["previous_hours"], 24)


class ConcurrentWorkersTests(TransactionTestCase):
    def test_workers_claim_a_delivery_only_once(self):
        actor = get_user_model().objects.create(
            username="operatore-queue", is_staff=True
        )
        election = create_election(actor)
        create_voter(election, 1)
        campaign = create_preview(election.pk, actor, MailKind.REMINDER, MESSAGE)
        confirm_campaign(election.pk, campaign.pk, actor)
        delivery = campaign.deliveries.get()
        barrier = Barrier(2, timeout=5)

        def start():
            close_old_connections()
            try:
                barrier.wait()
                return _claim(delivery.pk, election.pk) is not None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start) for _ in range(2)]
            self.assertEqual(sum(future.result(timeout=8) for future in futures), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.state, DeliveryState.SENDING)
