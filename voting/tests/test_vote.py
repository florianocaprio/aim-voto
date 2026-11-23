from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from voting import services
from voting.forms import RomeDateTimeField
from voting.models import (
    AuditEvent,
    Election,
    ElectionVoter,
    NamedBallot,
    SubmissionReceipt,
)

from .support import (
    add_members,
    election_with_motion,
    electorate_snapshot,
    open_election,
    redeem,
    review,
    staff,
)


@override_settings(REQUIRE_ADMIN_MFA=False)
class VotingViewsTests(TestCase):
    def setUp(self):
        self.actor = staff()
        self.election, self.question, self.options = election_with_motion(self.actor)
        self.voter, self.other = open_election(self.election, add_members(2))
        self.admin = Client()
        self.admin.force_login(self.actor)
        self.submit_url = reverse("voting:submit")

    def preview(self, revision, option=None, client=None):
        return review(client or self.client, revision, self.question, option)

    def test_redeeming_and_previewing_never_records_or_consumes_access(self):
        self.assertEqual(self.client.get(reverse("voting:access")).status_code, 200)
        self.assertEqual(redeem(self.client, self.voter).status_code, 200)
        self.assertEqual(self.client.get(reverse("voting:ballot")).status_code, 200)
        self.assertEqual(self.preview(0, self.options[0]).status_code, 200)
        self.assertFalse(NamedBallot.objects.exists())
        self.assertEqual(redeem(self.client, self.voter).status_code, 200)
        self.assertFalse(SubmissionReceipt.objects.exists())

    def test_electorate_constraint_rejects_same_member(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ElectionVoter.objects.create(
                election=self.election,
                member=self.voter.member,
                **electorate_snapshot(self.voter.member),
            )

    def test_whole_ballot_replacement_and_retries(self):
        redeem(self.client, self.voter)
        first = self.preview(0, self.options[0]).context["payload"]
        result = self.client.post(self.submit_url, {"payload": first})
        self.assertContains(result, "Versione 1")
        self.assertContains(result, "Scheda corrente")
        self.assertContains(
            self.client.post(self.submit_url, {"payload": first}), "Versione 1"
        )
        self.assertEqual(SubmissionReceipt.objects.count(), 1)

        abandoned = self.preview(1, self.options[1])
        self.assertEqual(abandoned.status_code, 200)
        self.assertContains(
            self.client.get(reverse("voting:ballot")), "Sostituisci il voto"
        )
        self.assertEqual(
            NamedBallot.objects.get().answers,
            {str(self.question.pk): [self.options[0].pk]},
        )
        invalid = self.client.post(
            reverse("voting:ballot"), {"revision": 1, f"q{self.question.pk}": [999999]}
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(NamedBallot.objects.get().revision, 1)

        second = self.preview(1, self.options[2]).context["payload"]
        self.assertContains(
            self.client.post(self.submit_url, {"payload": second}), "Versione 2"
        )
        self.assertContains(
            self.client.post(self.submit_url, {"payload": first}), "Scheda sostituita"
        )
        self.assertEqual(NamedBallot.objects.count(), 1)
        self.assertEqual(
            NamedBallot.objects.get().answers,
            {str(self.question.pk): [self.options[2].pk]},
        )

    def test_blank_is_counted_and_omitting_an_answer_is_allowed(self):
        redeem(self.client, self.voter)
        payload = self.preview(0).context["payload"]
        self.assertContains(
            self.client.post(self.submit_url, {"payload": payload}), "Scheda corrente"
        )
        self.assertEqual(NamedBallot.objects.get().answers, {})

    def test_wrong_voter_and_rotation(self):
        copy_url = reverse("voting:copy_link", args=[self.election.pk, self.voter.pk])
        first = self.admin.post(copy_url)
        self.assertIn("no-store", first.headers["Cache-Control"])
        self.assertEqual(first.json(), self.admin.post(copy_url).json())
        original_token = first.json()["url"].split("#", 1)[1]
        self.assertNotContains(
            self.admin.get(reverse("voting:voters", args=[self.election.pk])),
            original_token,
        )

        redeem(self.client, self.voter)
        payload = self.preview(0, self.options[0]).context["payload"]
        second_client = Client()
        redeem(second_client, self.other)
        self.assertEqual(
            second_client.post(self.submit_url, {"payload": payload}).status_code, 400
        )
        self.assertEqual(
            self.client.post(self.submit_url, {"payload": payload}).status_code, 200
        )
        rotate = reverse("voting:rotate", args=[self.election.pk, self.voter.pk])
        self.assertEqual(
            self.admin.post(
                rotate, {"reason": "esposizione", "confirm": True}
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(self.submit_url, {"payload": payload}).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("voting:access"), {"token": original_token}
            ).status_code,
            404,
        )
        self.assertEqual(NamedBallot.objects.count(), 1)
        self.assertNotIn(original_token, str(list(AuditEvent.objects.values())))
        audit = AuditEvent.objects.get(action="revoca_link")
        self.assertEqual(audit.details["motivo"], "esposizione")

    def test_voter_detail_opens_from_name_and_requires_staff(self):
        detail_url = reverse(
            "voting:voter_detail", args=[self.election.pk, self.voter.pk]
        )
        voter_list = self.admin.get(reverse("voting:voters", args=[self.election.pk]))
        self.assertContains(voter_list, f'href="{detail_url}"')
        detail = self.admin.get(detail_url)
        self.assertContains(detail, "Copia link personale")
        self.assertContains(detail, "aggiungi un cellulare valido")
        self.assertNotContains(detail, services.personal_url(self.voter))
        self.assertIn("/account/accesso/", self.client.get(detail_url).url)
        other_election, _, _ = election_with_motion(
            self.actor, "Consultazione distinta"
        )
        self.assertEqual(
            self.admin.get(
                reverse("voting:voter_detail", args=[other_election.pk, self.voter.pk])
            ).status_code,
            404,
        )

    def test_whatsapp_action_copies_current_link_and_opens_phone_only_url(self):
        self.voter.phone = "+1 (202) 555-0101"
        self.voter.save(update_fields=["phone"])
        args = [self.election.pk, self.voter.pk]
        detail_url = reverse("voting:voter_detail", args=args)
        copy_url = reverse("voting:copy_link", args=args)
        self.assertContains(
            self.admin.get(detail_url), "Copia messaggio e apri WhatsApp Web"
        )
        response = self.admin.post(copy_url, {"whatsapp_phone": "12025550101"})
        self.assertEqual(
            response.json(),
            {
                "url": services.personal_url(self.voter),
                "whatsapp_url": "https://web.whatsapp.com/send?phone=12025550101",
            },
        )
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(
            AuditEvent.objects.filter(action="copia_link", voter=self.voter).count(), 1
        )

    def test_whatsapp_rejects_missing_or_changed_phone_and_ended_election(self):
        args = [self.election.pk, self.voter.pk]
        copy_url = reverse("voting:copy_link", args=args)
        data = {"whatsapp_phone": "12025550101"}
        self.assertEqual(self.admin.post(copy_url, data).status_code, 409)
        self.voter.phone = "001 202-555-0102"
        self.voter.save(update_fields=["phone"])
        self.assertEqual(self.admin.post(copy_url, data).status_code, 409)
        Election.objects.filter(pk=self.election.pk).update(
            status=Election.Status.CLOSED
        )
        data["whatsapp_phone"] = "12025550102"
        self.assertEqual(self.admin.post(copy_url, data).status_code, 409)
        self.assertNotContains(
            self.admin.get(reverse("voting:voter_detail", args=args)),
            "Copia messaggio e apri WhatsApp Web",
        )
        self.assertFalse(AuditEvent.objects.filter(action="copia_link").exists())

    def test_object_access_and_reports(self):
        other_election, _, _ = election_with_motion(
            self.actor, "Consultazione distinta"
        )
        copy_url = reverse("voting:copy_link", args=[other_election.pk, self.voter.pk])
        self.assertEqual(self.admin.post(copy_url).status_code, 404)
        self.assertIn("/account/accesso/", self.client.post(copy_url).url)
        regular = get_user_model().objects.create(username="nonstaff")
        self.client.force_login(regular)
        self.assertEqual(self.client.get(reverse("voting:dashboard")).status_code, 403)
        results_url = reverse("voting:results", args=[self.election.pk])
        self.assertEqual(self.admin.get(results_url).status_code, 404)
        for kind in ("risultati-csv", "partecipazione-csv", "report-pdf"):
            self.assertEqual(
                self.admin.get(
                    reverse("voting:export", args=[self.election.pk, kind])
                ).status_code,
                404,
            )
        redeem(self.client, self.voter)
        payload = self.preview(0, self.options[0]).context["payload"]
        self.assertEqual(
            self.client.post(self.submit_url, {"payload": payload}).status_code, 200
        )
        pending = self.preview(1, self.options[2]).context["payload"]
        for action in ("chiudi", "scrutina"):
            url = reverse("voting:transition", args=[self.election.pk, action])
            self.assertEqual(self.admin.post(url, {"confirm": "yes"}).status_code, 302)
        self.assertEqual(
            self.client.post(self.submit_url, {"payload": pending}).status_code, 409
        )
        self.assertContains(
            self.client.get(reverse("voting:ballot")), "Votazione chiusa"
        )
        self.assertContains(
            self.client.post(self.submit_url, {"payload": payload}), "Scheda corrente"
        )
        self.assertContains(self.admin.get(results_url), "Favorevole")
        token = services.personal_url(self.voter).split("#", 1)[1].encode()
        for kind in ("risultati-csv", "partecipazione-csv", "report-pdf"):
            result = self.admin.get(
                reverse("voting:export", args=[self.election.pk, kind])
            )
            self.assertEqual(result.status_code, 200)
            data = (
                b"".join(result.streaming_content)
                if result.streaming
                else result.content
            )
            self.assertNotIn(token, data)
            if kind == "report-pdf":
                self.assertTrue(data.startswith(b"%PDF-"))

    def test_secret_refused_by_form_and_database(self):
        new_url = reverse("voting:create")
        response = self.admin.post(
            new_url,
            {
                "title": "Titolo",
                "mode": "secret",
                "closes_at": "2035-01-01T10:00",
            },
        )
        self.assertContains(response, "Voto segreto")
        self.assertFalse(Election.objects.filter(title="Titolo").exists())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Election.objects.filter(pk=self.election.pk).update(mode="secret")

    def test_deadline_without_scheduler_protects_previous_ballot(self):
        redeem(self.client, self.voter)
        first = self.preview(0, self.options[0]).context["payload"]
        self.client.post(self.submit_url, {"payload": first})
        pending = self.preview(1, self.options[1]).context["payload"]
        Election.objects.filter(pk=self.election.pk).update(
            closes_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(
            self.client.post(self.submit_url, {"payload": pending}).status_code, 409
        )
        self.assertEqual(NamedBallot.objects.get().revision, 1)
        self.election.refresh_from_db()
        self.assertEqual(self.election.status, Election.Status.OPEN)

    def test_ambiguous_and_nonexistent_rome_wall_times(self):
        for date in ("2026-03-29T02:30", "2026-10-25T02:30", "2026-10-25T02:30+02:00"):
            with self.assertRaises(ValidationError):
                RomeDateTimeField().clean(date)
        self.assertEqual(
            RomeDateTimeField().clean("2026-10-25T03:30").utcoffset(),
            timedelta(hours=1),
        )

    def test_csrf_required_for_access_and_confirmation(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(redeem(client, self.voter).status_code, 403)
        self.assertEqual(client.post(self.submit_url, {"payload": ""}).status_code, 403)

    def test_duplicate_request_id_cannot_change_payload(self):
        args = (self.voter.pk, self.election.pk, 1, uuid4(), 0)
        services.record_ballot(*args, {str(self.question.pk): [self.options[0].pk]})
        with self.assertRaisesMessage(ValidationError, "appartiene a un altro invio"):
            services.record_ballot(*args, {})
