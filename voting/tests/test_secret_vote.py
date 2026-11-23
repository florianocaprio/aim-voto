import json
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.crypto import salted_hmac

from mailings.campaigns import recipients
from mailings.models import MailKind
from voting import assembly_report, reports, services
from voting.models import (
    Delegation,
    Election,
    NamedBallot,
    SecretBallot,
    SubmissionReceipt,
    VoterPresence,
)

from .support import (
    add_members,
    election_with_motion,
    open_election,
    redeem,
    review,
    staff,
)


@override_settings(REQUIRE_ADMIN_MFA=False)
class SecretVoteTests(TestCase):
    def setUp(self):
        self.staff = staff()
        self.admin = Client()
        self.admin.force_login(self.staff)
        self.election, self.question, self.options = election_with_motion(self.staff)
        self.election.mode = Election.Mode.SECRET
        self.election.allow_replacement = False
        self.election.allow_delegations = True
        self.election.save()
        self.first, self.delegate, self.grantor, self.absent = open_election(
            self.election, add_members(4)
        )
        self.submit_url = reverse("voting:submit")
        redeem(self.client, self.first)

    def transition(self, action):
        return self.admin.post(
            reverse("voting:transition", args=[self.election.pk, action]),
            {"confirm": "yes"},
        )

    def test_secret_direct_and_proxy_acquisition_and_reports(self):
        payload = review(self.client, 0, self.question, self.options[0]).context[
            "payload"
        ]
        for _ in range(2):
            self.assertContains(
                self.client.post(self.submit_url, {"payload": payload}),
                "Scheda corrente",
            )
        receipt = SubmissionReceipt.objects.get(voter=self.first)
        participation_only = salted_hmac(
            "aim-voto.submission",
            json.dumps([self.first.pk, str(receipt.request_id), 0]),
            algorithm="sha256",
        ).hexdigest()
        self.assertEqual(receipt.fingerprint, participation_only)
        self.assertEqual(SecretBallot.objects.count(), 1)
        self.assertNotContains(
            self.client.get(reverse("voting:ballot"), {"modifica": 1}),
            "Sostituisci il voto",
        )
        with self.assertRaises(ValidationError):
            services.record_ballot(
                self.first.pk,
                self.election.pk,
                self.first.credential.version,
                uuid4(),
                1,
                {str(self.question.pk): [self.options[1].pk]},
            )

        grantor = Client()
        proxy = Client()
        redeem(grantor, self.grantor)
        redeem(proxy, self.delegate)
        grantor.post(
            reverse("voting:assembly"),
            {"action": "delegate", "external_id": self.delegate.external_id},
        )
        delegation = Delegation.objects.get(grantor=self.grantor)
        self.admin.post(
            reverse(
                "voting:staff_delegation",
                args=[self.election.pk, delegation.pk, "approve"],
            )
        )
        blocked = review(grantor, 0, self.question, self.options[1])
        self.assertEqual(blocked.status_code, 409)
        proxy_path = reverse("voting:proxy_ballot", args=[delegation.pk])
        payload = proxy.post(proxy_path, {"revision": 0}).context["payload"]
        self.assertContains(
            proxy.post(self.submit_url, {"payload": payload}),
            "Schede acquisite per 2 di 4",
        )
        grantor.post(reverse("voting:voter_revoke", args=[delegation.pk]))
        delegation.refresh_from_db()
        self.assertEqual(delegation.state, Delegation.State.APPROVED)
        self.assertFalse(NamedBallot.objects.exists())
        self.assertEqual(
            set(VoterPresence.objects.values_list("voter_id", flat=True)),
            {self.first.pk, self.delegate.pk},
        )
        self.assertEqual(
            {field.name for field in SecretBallot._meta.concrete_fields},
            {"id", "election", "ciphertext"},
        )
        ciphertexts = list(SecretBallot.objects.values_list("ciphertext", flat=True))
        self.assertEqual(len(ciphertexts[0]), len(ciphertexts[1]))
        self.assertNotEqual(bytes(ciphertexts[0]), bytes(ciphertexts[1]))
        self.assertEqual(assembly_report.participation(self.election)["represented"], 1)
        self.assertTrue(
            recipients(self.election, MailKind.REMINDER)
            .get(pk=self.grantor.pk)
            .already_voted
        )
        self.assertFalse(
            recipients(self.election, MailKind.REMINDER)
            .get(pk=self.absent.pk)
            .already_voted
        )
        with self.assertRaises(ValidationError):
            reports.tally(self.election)
        self.assertEqual(self.transition("chiudi").status_code, 302)
        self.assertEqual(self.transition("scrutina").status_code, 302)
        self.election.refresh_from_db()
        tally = reports.tally(self.election)
        self.assertEqual(tally["blank"], 1)
        self.assertEqual(
            dict(tally["rows"][0]["choices"]),
            {"Favorevole": 1, "Contrario": 0, "Astenuto": 0},
        )
        results_body = self.election.campaigns.get(kind=MailKind.RESULTS).body
        self.assertIn("Favorevole: 1", results_body)
        self.assertNotIn(self.first.external_id, results_body)
        export = self.admin.get(
            reverse("voting:export", args=[self.election.pk, "partecipazione-csv"])
        )
        self.assertIn("aventi-diritto.csv", export["Content-Disposition"])
        self.assertContains(export, self.absent.external_id)
        self.assertNotContains(export, "Ha votato")
        self.assertNotContains(export, "Favorevole")
        detail = self.admin.get(
            reverse("voting:voter_detail", args=[self.election.pk, self.first.pk])
        )
        self.assertContains(detail, "Ha votato")
        self.assertNotContains(detail, "Favorevole")

    def test_corrupt_ciphertext_keeps_scrutiny_unpublished(self):
        payload = review(self.client, 0, self.question, self.options[0]).context[
            "payload"
        ]
        self.client.post(self.submit_url, {"payload": payload})
        SecretBallot.objects.update(ciphertext=b"scheda-cifrata-alterata")
        self.assertEqual(self.transition("chiudi").status_code, 302)
        self.assertContains(self.transition("scrutina"), "Scrutinio sospeso")
        self.election.refresh_from_db()
        self.assertEqual(self.election.status, Election.Status.CLOSED)
        self.assertFalse(self.election.campaigns.filter(kind=MailKind.RESULTS).exists())
