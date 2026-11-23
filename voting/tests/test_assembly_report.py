import csv
from base64 import b64encode
from hashlib import sha256
from io import BytesIO, StringIO
from zipfile import ZipFile

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from voting import assembly_report, reports
from voting.models import (
    AssemblyQuorum,
    AuditEvent,
    Choice,
    Delegation,
    Election,
    NamedBallot,
    Question,
    VoterPresence,
)

from .support import add_members, election_with_motion, open_election, staff


class QuorumArithmeticTests(SimpleTestCase):
    def test_absolute_and_fractional_thresholds_use_exact_integer_boundaries(self):
        for numerator, denominator, eligible, inclusive, strict in (
            (1, 2, 7, 4, 4),
            (1, 2, 8, 4, 5),
            (2, 3, 6, 4, 5),
            (2, 3, 7, 5, 5),
            (4, None, 99, 4, 5),
        ):
            with self.subTest(
                numerator=numerator, denominator=denominator, eligible=eligible
            ):
                quorum = AssemblyQuorum(
                    threshold_numerator=numerator, threshold_denominator=denominator
                )
                self.assertEqual(quorum.minimum_count(eligible), inclusive)
                quorum.comparison = AssemblyQuorum.Comparison.GREATER
                self.assertEqual(quorum.minimum_count(eligible), strict)


@override_settings(REQUIRE_ADMIN_MFA=False)
class AssemblyReportTests(TestCase):
    def setUp(self):
        self.admin = staff()
        self.client.force_login(self.admin)
        self.election, self.question, self.options = election_with_motion(self.admin)
        self.form_url = reverse("voting:assembly_report", args=[self.election.pk])
        self.values = {
            "basis": AssemblyQuorum.Basis.WITH_PROXY,
            "comparison": AssemblyQuorum.Comparison.AT_LEAST,
            "threshold_numerator": 1,
            "threshold_denominator": 2,
            "reference": "Articolo dimostrativo",
        }

    def url(self, extension):
        return reverse(
            "voting:export",
            args=[self.election.pk, f"rapporto-assembleare-{extension}"],
        )

    def test_register_quorum_and_keep_publication_guard(self):
        self.assertNotContains(self.client.get(self.form_url), "Scarica rapporto PDF")
        invalid = self.client.post(
            self.form_url, {**self.values, "threshold_numerator": 3}
        )
        self.assertContains(
            invalid, "Il denominatore deve essere almeno pari al numeratore."
        )
        self.assertFalse(AssemblyQuorum.objects.exists())
        self.assertRedirects(
            self.client.post(self.form_url, self.values), self.form_url
        )
        rule = AssemblyQuorum.objects.get(election=self.election)
        self.assertEqual(rule.updated_by, self.admin)
        event = AuditEvent.objects.get(
            action="quorum_assembleare_registrato", election=self.election
        )
        self.assertEqual(event.details["denominatore"], 2)
        for status in (
            Election.Status.OPEN,
            Election.Status.CLOSED,
            Election.Status.CANCELLED,
        ):
            Election.objects.filter(pk=self.election.pk).update(status=status)
            self.assertEqual(self.client.get(self.url("pdf")).status_code, 404)
            self.assertEqual(self.client.get(self.url("zip")).status_code, 404)
        self.assertEqual(self.client.get(self.form_url).status_code, 404)

    def set_report_data(self):
        for index in range(2, 5):
            question = Question.objects.create(
                election=self.election,
                text=f"Mozione dimostrativa {index}",
                kind=Question.Kind.MOTION,
            )
            Choice.objects.create(question=question, text="Favorevole")
        voters = open_election(self.election, add_members(8))
        stamp = timezone.now()
        for index in (0, 1, 3):
            VoterPresence.objects.create(voter=voters[index], confirmed_at=stamp)
        for index in (1, 2, 4):
            Delegation.objects.create(
                grantor=voters[index],
                delegate=voters[0],
                state=Delegation.State.APPROVED,
                requested_at=stamp,
            )
        Delegation.objects.create(
            grantor=voters[1],
            delegate=voters[0],
            state=Delegation.State.REVOKED,
            requested_at=stamp,
        )
        answers = {str(self.question.pk): [self.options[0].pk]}
        for index in (0, 5, 6):
            NamedBallot.objects.create(voter=voters[index], answers=answers)
        NamedBallot.objects.create(voter=voters[1], cast_by=voters[0], answers=answers)
        NamedBallot.objects.create(voter=voters[2], cast_by=voters[0], answers={})
        Election.objects.filter(pk=self.election.pk).update(
            status=Election.Status.TALLIED, ended_at=stamp
        )
        self.election.refresh_from_db()

    def test_participation_uses_distinct_people_and_only_exercised_representations(
        self,
    ):
        self.set_report_data()
        counts = assembly_report.participation(self.election)
        self.assertEqual(counts["present"], 3)
        self.assertEqual(counts["participants"], 5)
        self.assertEqual(counts["direct"], 3)
        self.assertEqual(counts["represented"], 2)
        self.assertEqual(counts["approved_unused"], 1)
        self.assertEqual(counts["overlap"], 1)
        self.assertEqual(counts["with_proxy"], 4)
        total = reports.tally(self.election)
        self.assertEqual(len(total["rows"]), 4)
        self.assertEqual(total["rows"][0]["proxy_answered"], 1)
        rule = AssemblyQuorum(**self.values)
        self.assertEqual(
            assembly_report.quorum_result(rule, counts, 8),
            {"counted": 4, "minimum": 4, "met": True},
        )
        rule.basis = AssemblyQuorum.Basis.PRESENCES
        self.assertEqual(
            assembly_report.quorum_result(rule, counts, 8),
            {"counted": 3, "minimum": 4, "met": False},
        )
        self.assertIsNone(assembly_report.quorum_result(None, counts, 8))

    def test_zip_manifest_matches_every_file_and_direct_pdf_has_a_digest(self):
        self.set_report_data()
        self.assertEqual(Client().get(self.url("pdf")).status_code, 302)
        self.assertEqual(Client().get(self.url("zip")).status_code, 302)
        self.assertContains(self.client.get(self.form_url), "Scarica rapporto PDF")
        response = self.client.get(self.url("pdf"))
        pdf = b"".join(response.streaming_content)
        self.assertIn(assembly_report.PDF_FILENAME, response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(
            response["Content-Digest"],
            f"sha-256=:{b64encode(sha256(pdf).digest()).decode('ascii')}:",
        )
        self.assertRedirects(
            self.client.post(self.form_url, self.values), self.form_url
        )
        archive_response = self.client.get(self.url("zip"))
        self.assertIn(
            assembly_report.ZIP_FILENAME, archive_response["Content-Disposition"]
        )
        with ZipFile(BytesIO(b"".join(archive_response.streaming_content))) as archive:
            manifest = dict(
                (filename, digest)
                for digest, filename in (
                    line.split("  ", 1)
                    for line in archive.read("SHA256SUMS.txt")
                    .decode("ascii")
                    .splitlines()
                )
            )
            self.assertEqual(
                set(archive.namelist()), set(manifest) | {"SHA256SUMS.txt"}
            )
            self.assertEqual(
                set(manifest),
                {
                    assembly_report.PDF_FILENAME,
                    "risultati.csv",
                    "partecipazione.csv",
                    "presenze.csv",
                    "deleghe.csv",
                    "scrutinio.pdf",
                },
            )
            for filename, digest in manifest.items():
                self.assertEqual(sha256(archive.read(filename)).hexdigest(), digest)
            proxy_rows = list(
                csv.DictReader(
                    StringIO(archive.read("deleghe.csv").decode("utf-8-sig")),
                    delimiter=";",
                )
            )
            used = {
                row["Matricola delegante"]
                for row in proxy_rows
                if row["Esercitata con scheda acquisita"] == "Sì"
            }
            self.assertEqual(used, {"TEST-1", "TEST-2"})
            self.assertEqual(
                next(row for row in proxy_rows if row["Stato"] == "Revocata")[
                    "Esercitata con scheda acquisita"
                ],
                "No",
            )
