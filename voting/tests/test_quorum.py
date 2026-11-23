import json
from io import BytesIO
from zipfile import ZipFile

from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mailings.models import ReminderCampaign
from voting import quorum, reports
from voting.models import (
    AssemblyKind,
    ConstitutiveQuorum,
    Convocation,
    Delegation,
    DeliberationKind,
    DeliberativeMajority,
    NamedBallot,
    Question,
    QuorumBasis,
    VotingQuorum,
)

from .support import add_members, election_with_motion, open_election, redeem, staff


class DeliberativeMajorityTests(SimpleTestCase):
    def test_three_criteria_for_75_participants(self):
        rules = (
            (DeliberativeMajority.VOTES_CAST, "", ""),
            (DeliberativeMajority.PRESENT_ABSOLUTE, "", ""),
            (DeliberativeMajority.QUALIFIED, "2/3", QuorumBasis.PARTICIPANTS),
        )
        for favorable, against, expected in (
            (35, 20, [True, False, False]),
            (38, 17, [True, True, False]),
        ):
            with self.subTest(favorable=favorable):
                outcomes = [
                    quorum.deliberation_outcome(
                        quorum.majority_rule(*arguments),
                        arguments[0],
                        True,
                        eligible=90,
                        members=100,
                        participants=75,
                        favorable=favorable,
                        against=against,
                    )
                    for arguments in rules
                ]
                self.assertEqual([item["approved"] for item in outcomes], expected)
                self.assertEqual([item["minimum"] for item in outcomes], [28, 38, 50])
                self.assertEqual(
                    [item["denominator"] for item in outcomes], [55, 75, 75]
                )

    def test_constitutive_presets_and_boundaries(self):
        for kind, convocation, eligible, minimum in (
            (AssemblyKind.ORDINARY, Convocation.FIRST, 100, 51),
            (AssemblyKind.ORDINARY, Convocation.SECOND, 100, 0),
            (AssemblyKind.EXTRAORDINARY, Convocation.SECOND, 90, 30),
        ):
            rule = quorum.constitution_rule(kind, convocation)
            self.assertEqual(rule.minimum(eligible), minimum)
            self.assertFalse(
                quorum.constitution_outcome(rule, eligible, max(0, minimum - 1))["met"]
            )
            self.assertTrue(
                quorum.constitution_outcome(rule, eligible, max(1, minimum))["met"]
            )
        self.assertFalse(
            quorum.constitution_outcome(
                quorum.question_constitution_rule(ConstitutiveQuorum.ABSOLUTE), 100, 50
            )["met"]
        )

    def test_ordinary_statutory_and_dissolution_presets(self):
        for (
            matter,
            mode,
            fraction,
            basis,
            participants,
            favorable,
            against,
            minimum,
            approved,
        ) in (
            (DeliberationKind.ORDINARY, "", "", "", 20, 8, 7, 8, True),
            (DeliberationKind.ORDINARY, "", "", "", 20, 10, 10, 11, False),
            (DeliberationKind.ORDINARY, "", "", "", 20, 0, 0, 1, False),
            (DeliberationKind.STATUTE, "", "", "", 30, 16, 0, 16, True),
            (
                DeliberationKind.STATUTE,
                DeliberativeMajority.QUALIFIED,
                "2/3",
                QuorumBasis.PARTICIPANTS,
                30,
                19,
                0,
                20,
                False,
            ),
            (
                DeliberationKind.STATUTE,
                DeliberativeMajority.QUALIFIED,
                "2/3",
                QuorumBasis.PARTICIPANTS,
                30,
                20,
                0,
                20,
                True,
            ),
            (DeliberationKind.DISSOLUTION, "", "", "", 90, 74, 0, 75, False),
            (DeliberationKind.DISSOLUTION, "", "", "", 90, 75, 0, 75, True),
        ):
            with self.subTest(matter=matter, favorable=favorable, mode=mode):
                selected, rule = quorum.deliberation_rule(matter, mode, fraction, basis)
                counts = dict(
                    eligible=90,
                    members=100,
                    participants=participants,
                    favorable=favorable,
                    against=against,
                )
                outcome = quorum.deliberation_outcome(rule, selected, True, **counts)
                self.assertEqual(outcome["minimum"], minimum)
                self.assertEqual(outcome["approved"], approved)
                self.assertFalse(
                    quorum.deliberation_outcome(rule, selected, False, **counts)[
                        "approved"
                    ]
                )

    def test_qualified_requires_explicit_basis_and_protects_dissolution(self):
        for fraction, basis in (
            ("2/3", ""),
            (">2/3", QuorumBasis.MEMBERS),
            ("libero", QuorumBasis.ELIGIBLE),
        ):
            with self.assertRaises(ValidationError):
                quorum.majority_rule(DeliberativeMajority.QUALIFIED, fraction, basis)
        for fraction, basis in (
            ("2/3", QuorumBasis.MEMBERS),
            ("3/4", QuorumBasis.ELIGIBLE),
        ):
            with self.assertRaises(ValidationError):
                quorum.deliberation_rule(
                    DeliberationKind.DISSOLUTION,
                    DeliberativeMajority.QUALIFIED,
                    fraction,
                    basis,
                )


@override_settings(REQUIRE_ADMIN_MFA=False)
class QuestionQuorumWorkflowTests(TestCase):
    def setUp(self):
        self.actor = staff()
        self.client.force_login(self.actor)
        self.election, self.motion, self.options = election_with_motion(self.actor)
        self.pk = self.election.pk
        self.add_url = reverse("voting:question_add", args=[self.pk])
        self.definition = {
            "text": "Mozione con soglia autonoma",
            "kind": Question.Kind.MOTION,
            "max_choices": 1,
            "constitutive_quorum": VotingQuorum.TWO_THIRDS,
            "deliberative_majority": DeliberativeMajority.VOTES_CAST,
        }

    def transition(self, action):
        return self.client.post(
            reverse("voting:transition", args=[self.pk, action]), {"confirm": "yes"}
        )

    def test_question_form_saves_threshold_and_independent_criterion(self):
        form = self.client.get(self.add_url)
        for label in (
            "Quorum del quesito",
            "Criterio deliberativo",
        ):
            self.assertContains(form, label)
        fields = form.context["form"].fields
        self.assertEqual(
            [label for _, label in fields["constitutive_quorum"].choices],
            ["Libero", "3/4", "2/3", "1/2", "1/3"],
        )
        self.assertEqual(
            [label for _, label in fields["deliberative_majority"].choices],
            [
                "Voti espressi",
                "Maggioranza assoluta dei presenti",
                "Maggioranza qualificata",
            ],
        )
        self.assertContains(
            self.client.post(
                self.add_url,
                {
                    **self.definition,
                    "deliberative_majority": DeliberativeMajority.QUALIFIED,
                    "majority_override": "2/3",
                },
            ),
            "Indica la frazione e il denominatore",
        )
        self.assertEqual(
            self.client.post(self.add_url, self.definition).status_code, 302
        )
        question = self.election.questions.last()
        self.assertEqual(question.voting_quorum, VotingQuorum.TWO_THIRDS)
        self.assertEqual(
            question.deliberative_majority, DeliberativeMajority.VOTES_CAST
        )
        edit = reverse("voting:question_edit", args=[self.pk, question.pk])
        self.assertContains(
            self.client.get(edit),
            '<option value="2/3" selected>2/3</option>',
            html=True,
        )
        first, second = open_election(self.election, add_members(2))
        self.assertEqual(self.client.post(edit, self.definition).status_code, 404)
        favorable, _against, abstained = question.choices.all()
        NamedBallot.objects.create(
            voter=first, answers={str(question.pk): [favorable.pk]}
        )
        NamedBallot.objects.create(voter=second, answers={})
        result = reports.results_with_outcomes(self.election)["rows"][1]
        self.assertEqual(result["outcome_label"], "Quorum del quesito non raggiunto")
        NamedBallot.objects.filter(voter=second).update(
            answers={str(question.pk): [abstained.pk]}
        )
        result = reports.results_with_outcomes(self.election)["rows"][1]
        self.assertEqual(result["quorum"]["counted"], 2)
        self.assertEqual(result["deliberative"]["denominator"], 1)
        self.assertEqual(result["outcome_label"], "Approvata")

    def test_summary_displays_effective_quorum_on_each_question(self):
        self.election.voting_quorum = VotingQuorum.FREE
        self.election.save(update_fields=["voting_quorum"])
        self.motion.voting_quorum = VotingQuorum.THREE_QUARTERS
        self.motion.save(update_fields=["voting_quorum"])
        second = self.election.questions.create(
            text="Quesito a quorum libero", kind=Question.Kind.MOTION
        )
        open_election(self.election, add_members(20))
        response = self.client.get(reverse("voting:detail", args=[self.pk]))
        summaries = {
            question.pk: rule for question, rule in response.context["questions"]
        }
        self.assertEqual(summaries[self.motion.pk]["minimum"], 15)
        self.assertEqual(summaries[second.pk]["minimum"], 0)
        self.assertContains(
            response,
            "Quorum del quesito · 3/4 degli aventi diritto · Minimo 15 risposte",
        )
        self.assertContains(response, "Quorum del quesito · Libero")
        self.assertNotContains(response, "Quorum della votazione")

    def test_verified_assembly_deduplicates_and_versions_results_and_reports(self):
        self.election.assembly_type = AssemblyKind.ORDINARY
        self.election.convocation = Convocation.SECOND
        self.election.total_members = 4
        self.election.allow_custom_quorum = True
        self.election.save()
        self.assertEqual(
            self.client.post(
                self.add_url,
                {
                    **self.definition,
                    "deliberative_majority": DeliberativeMajority.PRESENT_ABSOLUTE,
                },
            ).status_code,
            302,
        )
        second_motion = self.election.questions.last()
        second_options = list(second_motion.choices.all())
        first, represented, absent = open_election(self.election, add_members(3))
        delegation = Delegation.objects.create(
            grantor=represented,
            delegate=first,
            state=Delegation.State.APPROVED,
            requested_at=timezone.now(),
        )
        NamedBallot.objects.create(
            voter=first,
            answers={
                str(self.motion.pk): [self.options[0].pk],
                str(second_motion.pk): [second_options[0].pk],
            },
        )
        NamedBallot.objects.create(
            voter=represented,
            cast_by=first,
            answers={
                str(self.motion.pk): [self.options[2].pk],
                str(second_motion.pk): [second_options[2].pk],
            },
        )
        self.assertEqual(self.transition("chiudi").status_code, 302)
        self.assertContains(
            self.transition("scrutina"),
            "Registra la verifica assembleare",
        )
        verify_url = reverse("voting:verify_assembly", args=[self.pk])
        fields = {
            "expected_revision": 0,
            "president": "Presidente Fittizio",
            "reference": "Verbale TEST-1",
            "present": [first.pk, represented.pk],
            "delegations": [delegation.pk],
            "attest": "on",
        }
        self.assertEqual(self.client.post(verify_url, fields).status_code, 302)
        initial = self.election.assembly_verifications.get()
        self.assertEqual(initial.participation["recognized"], 2)
        self.assertEqual(initial.participation["represented_only"], 0)
        self.assertEqual(
            [row["outcome"] for row in initial.result["rows"]], ["approved", "rejected"]
        )
        self.assertEqual(self.transition("scrutina").status_code, 302)
        self.assertIn(
            "Maggioranza assoluta dei presenti",
            ReminderCampaign.objects.get(election=self.election).body,
        )

        voter = Client()
        redeem(voter, first)
        result = voter.get(reverse("voting:ballot"))
        self.assertContains(result, "Deliberazione non approvata")
        self.assertNotContains(result, "Presidente Fittizio")
        csv_response = self.client.get(
            reverse("voting:export", args=[self.pk, "risultati-csv"])
        )
        self.assertContains(csv_response, "Maggioranza assoluta dei presenti")
        zip_response = self.client.get(
            reverse("voting:export", args=[self.pk, "rapporto-assembleare-zip"])
        )
        with ZipFile(BytesIO(b"".join(zip_response.streaming_content))) as bundle:
            exported = json.loads(bundle.read("verifica-assembleare.json"))
            self.assertEqual(exported["verifiche"][0]["revision"], 1)
            self.assertTrue(
                bundle.read("Verbale_Assembleare_AIM_VOTO.pdf").startswith(b"%PDF")
            )

        correction = {
            **fields,
            "correction_reason": "Rettifica di prova",
            "present": [first.pk, absent.pk],
        }
        self.assertContains(
            self.client.post(verify_url, correction), "La verifica è stata aggiornata"
        )
        self.assertEqual(
            self.client.post(
                verify_url, {**correction, "expected_revision": 1}
            ).status_code,
            302,
        )
        latest, retained = self.election.assembly_verifications.all()
        self.assertEqual(latest.participation["recognized"], 3)
        self.assertEqual(latest.participation["represented_only"], 1)
        self.assertEqual(latest.result["rows"][1]["no_choice"], 1)
        self.assertEqual(retained.result, initial.result)
