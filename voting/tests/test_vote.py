from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from voting import reports, services
from voting.forms import RomeDateTimeField
from voting.models import (
    AuditEvent,
    Election,
    ElectionDocument,
    ElectionVoter,
    NamedBallot,
    Question,
    SecretBallot,
    SubmissionReceipt,
    VotingCredential,
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

    def test_delete_published_requires_staff_published_status_and_confirmation(self):
        pk = self.election.pk
        details = reverse("voting:detail", args=[pk])
        remove = reverse("voting:transition", args=[pk, "elimina-pubblicata"])
        for status in Election.Status:
            if status == Election.Status.TALLIED:
                continue
            with self.subTest(status=status):
                Election.objects.filter(pk=pk).update(status=status)
                self.assertNotContains(self.admin.get(details), "Elimina consultazione")
                self.assertEqual(self.admin.get(remove).status_code, 404)
                self.assertEqual(
                    self.admin.post(remove, {"confirm": "yes"}).status_code, 404
                )
        Election.objects.filter(pk=pk).update(status=Election.Status.TALLIED)
        regular = get_user_model().objects.create(username="socio-non-staff")
        self.client.force_login(regular)
        self.assertEqual(self.client.post(remove, {"confirm": "yes"}).status_code, 403)
        self.assertContains(self.admin.get(details), "Elimina consultazione")
        self.assertContains(
            self.admin.get(remove), "Confermo l’eliminazione definitiva"
        )
        self.assertContains(self.admin.post(remove), "Conferma l’operazione")
        self.assertTrue(Election.objects.filter(pk=pk).exists())
        self.assertFalse(
            AuditEvent.objects.filter(action="eliminazione_consultazione").exists()
        )

    def test_delete_published_removes_only_the_selected_election_and_its_records(self):
        for mode in Election.Mode:
            with self.subTest(mode=mode):
                election, question, options = election_with_motion(
                    self.actor, title=f"Consultazione di prova {mode}"
                )
                election.mode = mode
                election.save(update_fields=["mode"])
                (voter,) = open_election(election, [self.voter.member])
                ElectionDocument.objects.create(
                    election=election, name="Nota sintetica.pdf", content=b"%PDF-1.4"
                )
                token = services.personal_url(voter).split("#", 1)[1]
                redeem(self.client, voter)
                payload = review(self.client, 0, question, options[0]).context[
                    "payload"
                ]
                self.assertEqual(
                    self.client.post(self.submit_url, {"payload": payload}).status_code,
                    200,
                )
                for action in ("chiudi", "scrutina"):
                    url = reverse("voting:transition", args=[election.pk, action])
                    self.assertEqual(
                        self.admin.post(url, {"confirm": "yes"}).status_code, 302
                    )
                self.assertEqual(election.campaigns.get().deliveries.count(), 1)
                remove = reverse(
                    "voting:transition", args=[election.pk, "elimina-pubblicata"]
                )
                self.assertRedirects(
                    self.admin.post(remove, {"confirm": "yes"}),
                    reverse("voting:dashboard"),
                )
                self.assertEqual(self.admin.get(remove).status_code, 404)
                self.assertEqual(
                    self.client.post(
                        reverse("voting:access"), {"token": token}
                    ).status_code,
                    404,
                )
                self.assertFalse(Election.objects.filter(pk=election.pk).exists())
                self.assertFalse(ElectionVoter.objects.filter(pk=voter.pk).exists())
                self.assertFalse(
                    VotingCredential.objects.filter(voter_id=voter.pk).exists()
                )
                self.assertFalse(
                    SubmissionReceipt.objects.filter(voter_id=voter.pk).exists()
                )
                self.assertFalse(NamedBallot.objects.filter(voter_id=voter.pk).exists())
                self.assertFalse(
                    SecretBallot.objects.filter(election=election).exists()
                )
                self.assertFalse(
                    ElectionDocument.objects.filter(election=election).exists()
                )
                self.assertFalse(election.campaigns.exists())
                self.assertTrue(
                    AuditEvent.objects.filter(
                        action="eliminazione_consultazione",
                        actor=self.actor,
                        details={"consultazione": election.pk},
                    ).exists()
                )
        self.assertTrue(ElectionVoter.objects.filter(pk=self.voter.pk).exists())
        self.assertTrue(VotingCredential.objects.filter(voter=self.voter).exists())

    def test_motion_can_require_a_response_without_counting_omissions_on_other_questions(
        self,
    ):
        election = Election.objects.create(
            title="Consultazione con due mozioni",
            created_by=self.actor,
            closes_at=timezone.now() + timedelta(days=1),
        )
        add_question = reverse("voting:question_add", args=[election.pk])
        for text, kind in (
            ("Approvare la prima mozione?", Question.Kind.MOTION_REQUIRED),
            ("Approvare la seconda mozione?", Question.Kind.MOTION),
        ):
            response = self.admin.post(
                add_question, {"text": text, "kind": kind, "max_choices": 2}
            )
            self.assertEqual(response.status_code, 302)
        required, optional = election.questions.all()
        for question in (required, optional):
            self.assertEqual(question.max_choices, 1)
            self.assertEqual(
                list(question.choices.values_list("text", flat=True)),
                ["Favorevole", "Contrario", "Astenuto"],
            )

        first, second = open_election(election, [self.voter.member, self.other.member])
        redeem(self.client, first)
        ballot = reverse("voting:ballot")
        page = self.client.get(ballot)
        self.assertNotContains(page, f'name="q{required.pk}" value=""')
        self.assertContains(page, f'name="q{optional.pk}" value=""')
        for data in ({}, {f"q{required.pk}": ""}):
            invalid = self.client.post(ballot, {"revision": 0, **data})
            self.assertContains(
                invalid, "Seleziona una risposta per il quesito", status_code=400
            )
        with self.assertRaises(ValidationError):
            services.record_ballot(
                first.pk,
                election.pk,
                first.credential.version,
                uuid4(),
                0,
                {str(optional.pk): [optional.choices.first().pk]},
            )

        other = Client()
        redeem(other, second)
        for client, optional_answer in (
            (self.client, ""),
            (other, optional.choices.get(text="Astenuto").pk),
        ):
            preview = client.post(
                ballot,
                {
                    "revision": 0,
                    f"q{required.pk}": required.choices.first().pk,
                    f"q{optional.pk}": optional_answer,
                },
            )
            self.assertEqual(
                client.post(
                    self.submit_url, {"payload": preview.context["payload"]}
                ).status_code,
                200,
            )
        result = reports.tally(election)
        self.assertEqual(result["total"], 2)
        self.assertEqual(
            [(row["answered"], row["omitted"]) for row in result["rows"]],
            [(2, 0), (1, 1)],
        )
        self.assertEqual(dict(result["rows"][1]["choices"])["Astenuto"], 1)

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
        nominative = self.admin.get(
            reverse("voting:voter_detail", args=[self.election.pk, self.voter.pk])
        )
        self.assertContains(nominative, "Scelte della scheda nominativa")
        self.assertContains(nominative, self.question.text)
        self.assertContains(nominative, self.options[0].text)
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

    def test_replacement_setting_is_saved_in_both_voting_modes(self):
        new_url = reverse("voting:create")
        form = self.admin.get(new_url)
        self.assertContains(form, "Modalità di voto")
        self.assertContains(
            form,
            '<option value="secret">Segreto</option>',
            html=True,
        )
        self.assertNotContains(form, "Voto segreto · In preparazione")
        for mode in Election.Mode:
            for replacement in (True, False):
                with self.subTest(mode=mode, replacement=replacement):
                    title = f"Colloquio {mode} {replacement}"
                    fields = {
                        "title": title,
                        "mode": mode,
                        "closes_at": "2035-01-01T10:00",
                    }
                    if replacement:
                        fields["allow_replacement"] = "on"
                    self.assertEqual(self.admin.post(new_url, fields).status_code, 302)
                    election = Election.objects.get(title=title)
                    self.assertEqual(election.mode, mode)
                    self.assertEqual(election.allow_replacement, replacement)

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
