import json
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.crypto import salted_hmac

from mailings.campaigns import recipients
from mailings.models import MailKind
from voting import assembly_report, proxy_access, reports, services
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
        self.staff = staff(superadmin=True)
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
        electorate = reverse("voting:voters", args=[self.election.pk])
        self.assertContains(self.admin.get(electorate), "1 delega da votare")
        proxy_path = reverse("voting:proxy_ballot", args=[delegation.pk])
        payload = proxy.post(proxy_path, {"revision": 0}).context["payload"]
        self.assertContains(
            proxy.post(self.submit_url, {"payload": payload}),
            "Schede acquisite per 2 di 4",
        )
        voters = self.admin.get(electorate)
        self.assertContains(voters, "Ha votato per delega")
        self.assertNotContains(voters, "delega da votare")
        self.assertContains(
            proxy.get(reverse("voting:assembly")), 'class="button delegation-completed"'
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
        ballot = reverse("voting:ballot")
        self.assertContains(proxy.get(ballot), "Scrutinio pubblicato")
        scoped = Client()
        token = proxy_access.link(delegation, self.delegate.credential).split("#", 1)[1]
        self.assertEqual(
            scoped.post(reverse("voting:access"), {"token": token}).status_code, 200
        )
        summarized = scoped.get(proxy_path)
        self.assertContains(summarized, "Scrutinio pubblicato")
        self.assertContains(summarized, "Favorevole")
        self.assertNotContains(summarized, self.grantor.external_id)
        self.assertEqual(scoped.get(ballot).status_code, 403)
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

    def test_direct_and_proxy_replacement_count_only_the_current_secret_ballot(self):
        election, question, options = election_with_motion(
            self.staff, title="Sostituzioni segrete"
        )
        election.mode = Election.Mode.SECRET
        election.allow_replacement = True
        election.allow_delegations = True
        election.save()
        voter, delegate, grantor = open_election(
            election, [self.first.member, self.delegate.member, self.grantor.member]
        )
        delegation = Delegation.objects.create(
            delegate=delegate,
            grantor=grantor,
            state=Delegation.State.APPROVED,
            requested_at=services.database_now(),
        )
        personal = Client()
        proxy = Client()
        redeem(personal, voter)
        token = proxy_access.link(delegation, delegate.credential).split("#", 1)[1]
        proxy_url = reverse("voting:proxy_ballot", args=[delegation.pk])
        access = proxy.post(reverse("voting:access"), {"token": token})
        self.assertEqual(access.json()["ballot_url"], proxy_url)
        for client, ballot_url, choice in (
            (personal, reverse("voting:ballot"), options[1]),
            (proxy, proxy_url, options[2]),
        ):
            previews = []
            for revision, option in enumerate((options[0], choice)):
                pending = client.post(
                    ballot_url, {"revision": revision, f"q{question.pk}": option.pk}
                ).context["payload"]
                previews.append(pending)
                acquired = client.post(self.submit_url, {"payload": pending})
                self.assertContains(acquired, f"Versione {revision + 1}")
                self.assertContains(client.get(ballot_url), "Sostituisci il voto")
                editing = client.get(ballot_url, {"modifica": 1})
                self.assertContains(editing, "Compila di nuovo i quesiti")
                self.assertEqual(editing.context["questions"][0][1], set())
                urn = set(
                    SecretBallot.objects.filter(election=election).values_list(
                        "pk", flat=True
                    )
                )
                if revision == 0:
                    first_urn = urn
                else:
                    self.assertEqual(len(urn), len(first_urn))
                    self.assertEqual(len(first_urn - urn), 1)
            retry = client.post(self.submit_url, {"payload": previews[0]})
            self.assertContains(retry, "Scheda sostituita da un invio successivo")

        pending = review(personal, 2, question, options[0]).context["payload"]
        for action in ("chiudi", "scrutina"):
            response = self.admin.post(
                reverse("voting:transition", args=[election.pk, action]),
                {"confirm": "yes"},
            )
            self.assertEqual(response.status_code, 302)
        self.assertContains(
            personal.post(self.submit_url, {"payload": pending}),
            "Votazione chiusa",
            status_code=409,
        )
        election.refresh_from_db()
        self.assertFalse(NamedBallot.objects.filter(voter__election=election).exists())
        self.assertEqual(
            SubmissionReceipt.objects.filter(voter__election=election).count(), 4
        )
        self.assertEqual(
            list(election.participation_records().values_list("revision", flat=True)),
            [2, 2],
        )
        self.assertEqual(assembly_report.participation(election)["represented"], 1)
        data = reports.tally(election)
        self.assertEqual(data["total"], 2)
        self.assertEqual(
            dict(data["rows"][0]["choices"]),
            {"Favorevole": 0, "Contrario": 1, "Astenuto": 1},
        )

    def test_secret_links_identify_distinct_voters_and_invalidate_stale_forms(self):
        client = Client(enforce_csrf_checks=True)
        access = reverse("voting:access")
        ballot = reverse("voting:ballot")
        client.get(access)
        links = [
            self.admin.post(
                reverse("voting:copy_link", args=[self.election.pk, voter.pk])
            ).json()["url"]
            for voter in (self.first, self.delegate)
        ]
        self.assertTrue(links[0] != links[1])

        def csrf():
            return client.cookies[settings.CSRF_COOKIE_NAME].value

        def open_link(link, voter):
            response = client.post(
                access, {"token": link.split("#", 1)[1], "csrfmiddlewaretoken": csrf()}
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(client.get(ballot), f"Matricola {voter.external_id}")

        open_link(links[0], self.first)
        old_token = csrf()
        open_link(links[1], self.delegate)
        data = {"revision": 0, f"q{self.question.pk}": self.options[0].pk}
        rejected = client.post(ballot, data | {"csrfmiddlewaretoken": old_token})
        self.assertContains(rejected, "riapri il link personale", status_code=403)
        self.assertFalse(SubmissionReceipt.objects.exists())
        for link, voter in zip(
            reversed(links), (self.delegate, self.first), strict=True
        ):
            open_link(link, voter)
            preview = client.post(ballot, data | {"csrfmiddlewaretoken": csrf()})
            self.assertContains(preview, f"Matricola {voter.external_id}")
            receipt = client.post(
                self.submit_url,
                {"payload": preview.context["payload"], "csrfmiddlewaretoken": csrf()},
            )
            self.assertContains(receipt, f"Matricola {voter.external_id}")
            self.assertEqual(receipt.context["receipt"].voter_id, voter.pk)
        self.assertEqual(SubmissionReceipt.objects.count(), 2)

    def test_forms_open_before_csrf_rotation_require_reopening_the_link(self):
        delegation = Delegation.objects.create(
            grantor=self.grantor,
            delegate=self.delegate,
            state=Delegation.State.APPROVED,
            requested_at=services.database_now(),
        )
        client = Client()
        session = client.session
        session["vote_access"] = [
            self.first.pk,
            self.election.pk,
            self.first.credential.version,
        ]
        session["proxy_access"] = [
            self.delegate.pk,
            self.election.pk,
            self.delegate.credential.version,
            delegation.pk,
        ]
        session.save()
        for target in (
            reverse("voting:ballot"),
            reverse("voting:proxy_ballot", args=[delegation.pk]),
        ):
            with self.subTest(target=target):
                response = client.post(target, {"revision": 0})
                self.assertEqual(response.status_code, 403)
        self.assertFalse(SubmissionReceipt.objects.exists())
