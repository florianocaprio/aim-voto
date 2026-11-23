import csv
from base64 import b64encode
from datetime import date
from hashlib import sha256
from io import BytesIO, StringIO
from unittest.mock import patch
from zipfile import ZipFile

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from docx import Document
from docx.shared import Pt

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
    VotingQuorum,
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

    def test_minutes_page_requires_publication_for_downloads(self):
        rule = AssemblyQuorum.objects.create(election=self.election, **self.values)
        draft = self.client.get(self.form_url)
        self.assertContains(draft, "Verbale assembleare")
        self.assertNotContains(draft, "Scarica verbale PDF")
        self.assertNotContains(draft, "Scarica verbale Word")
        self.assertNotContains(draft, "Salva quorum")
        self.assertEqual(
            self.client.post(
                self.form_url, {"download": "pdf", "president": "Ada Demo"}
            ).status_code,
            404,
        )
        rule.refresh_from_db()
        self.assertEqual(rule.threshold_numerator, 1)
        for status in (
            Election.Status.OPEN,
            Election.Status.CLOSED,
            Election.Status.CANCELLED,
        ):
            Election.objects.filter(pk=self.election.pk).update(status=status)
            self.assertEqual(self.client.get(self.url("pdf")).status_code, 404)
            self.assertEqual(self.client.get(self.url("docx")).status_code, 404)
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
        published = self.client.get(self.form_url)
        self.assertContains(published, "Scarica verbale PDF")
        self.assertNotContains(published, "Partecipazioni da conteggiare")
        response = self.client.get(self.url("pdf"))
        pdf = b"".join(response.streaming_content)
        self.assertIn(assembly_report.PDF_FILENAME, response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(
            response["Content-Digest"],
            f"sha-256=:{b64encode(sha256(pdf).digest()).decode('ascii')}:",
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

    def test_minutes_distinguish_outcomes_and_include_signature_lines(self):
        self.set_report_data()
        self.election.voting_quorum = VotingQuorum.FREE
        self.election.save(update_fields=["voting_quorum"])
        second, third = self.election.questions.all()[1:3]
        third.voting_quorum = VotingQuorum.THREE_QUARTERS
        third.save(update_fields=["voting_quorum"])
        against = second.choices.create(text="Contrario")
        ballot = NamedBallot.objects.filter(voter__election=self.election).first()
        ballot.answers[str(second.pk)] = [against.pk]
        ballot.save(update_fields=["answers"])
        AssemblyQuorum.objects.create(election=self.election, **self.values)

        with patch("reportlab.rl_config.pageCompression", 0):
            response = self.client.get(self.url("pdf"))
            pdf = b"".join(response.streaming_content)
        for label in (
            b"VERBALE ASSEMBLEARE",
            b"(APPROVATA)",
            b"(NON APPROVATA)",
            b"QUORUM DEL QUESITO NON RAGGIUNTO",
            b"Firma del presidente",
            b"Firma del segretario",
            b"Appendice A - Aventi diritto al voto",
            b"Appendice B - Votanti e scelte per quesito",
        ):
            self.assertIn(label, pdf)
        signatures = pdf.index(b"Firma del presidente")
        roster = pdf.index(b"Appendice A - Aventi diritto al voto")
        named_votes = pdf.index(b"Appendice B - Votanti e scelte per quesito")
        self.assertLess(signatures, roster)
        self.assertIn(b"TEST-7", pdf[roster:named_votes])
        self.assertNotIn(b"Criterio assembleare storico", pdf)
        self.assertNotIn(b"Ordine del giorno", pdf)
        self.assertNotIn(b"(Discussione)", pdf)

    def test_signer_names_appear_next_to_their_signatures_in_both_downloads(self):
        self.set_report_data()
        names = {"president": " Ada Demo ", "secretary": "Elena Prova & Rossi"}
        self.assertEqual(
            Client().post(self.form_url, {"download": "pdf", **names}).status_code,
            302,
        )
        with patch("reportlab.rl_config.pageCompression", 0):
            for kind in ("pdf", "zip"):
                with self.subTest(kind=kind):
                    response = self.client.post(
                        self.form_url, {"download": kind, **names}
                    )
                    self.assertEqual(response.status_code, 200)
                    pdf = b"".join(response.streaming_content)
                    if kind == "zip":
                        with ZipFile(BytesIO(pdf)) as archive:
                            pdf = archive.read(assembly_report.PDF_FILENAME)
                    president = pdf.index(b"Firma del presidente")
                    secretary = pdf.index(b"Firma del segretario")
                    appendix = pdf.index(b"Appendice A")
                    self.assertIn(b"(Ada Demo)", pdf[president:secretary])
                    self.assertIn(b"(Elena Prova & Rossi)", pdf[secretary:appendix])
        page = self.client.get(self.form_url)
        self.assertContains(page, "Nome e cognome del presidente")
        self.assertContains(page, "Nome e cognome del segretario")
        self.assertContains(page, 'value="Ada Demo"')
        self.assertContains(page, 'value="Elena Prova &amp; Rossi"')

    def test_saved_signers_can_be_updated_and_are_specific_to_the_election(self):
        self.set_report_data()
        other, _, _ = election_with_motion(self.admin, "Seconda consultazione demo")
        other.status = Election.Status.TALLIED
        other.save(update_fields=["status"])

        response = self.client.post(
            self.form_url,
            {"download": "save", "president": "Ada Demo", "secretary": "Elena Prova"},
            follow=True,
        )
        self.assertRedirects(response, self.form_url)
        self.assertContains(response, "Dati del verbale salvati.")
        self.assertContains(response, 'value="Ada Demo"')
        self.assertContains(response, 'value="Elena Prova"')

        self.client.post(
            self.form_url,
            {"download": "save", "president": "Nicolò Fittizio", "secretary": ""},
        )
        page = self.client.get(self.form_url)
        self.assertContains(page, 'value="Nicolò Fittizio"')
        self.assertNotContains(page, "Elena Prova")
        with patch("reportlab.rl_config.pageCompression", 0):
            response = self.client.get(self.url("pdf"))
            pdf = b"".join(response.streaming_content)
        self.assertIn(b"Fittizio", pdf)
        self.assertNotIn(b"Elena Prova", pdf)
        other_page = self.client.get(reverse("voting:assembly_report", args=[other.pk]))
        self.assertEqual(other_page.context["form"]["president"].value(), "")
        self.assertEqual(other_page.context["form"]["secretary"].value(), "")

    def test_minutes_include_the_saved_sections_and_signing_details_in_order(self):
        self.set_report_data()
        agenda = "1. Proposta dimostrativa\n2. Approvazione del programma"
        discussion = "La discussione ha riguardato i materiali del programma."
        with patch("reportlab.rl_config.pageCompression", 0):
            response = self.client.post(
                self.form_url,
                {
                    "download": "zip",
                    "signing_city": "Roma",
                    "signing_date": "2026-11-23",
                    "agenda": agenda,
                    "discussion": discussion,
                },
            )
            with ZipFile(BytesIO(b"".join(response.streaming_content))) as archive:
                pdf = archive.read(assembly_report.PDF_FILENAME)
        labels = (
            b"Ordine del giorno",
            b"1. Proposta dimostrativa",
            b"2. Approvazione del programma",
            b"(Discussione)",
            discussion.encode("ascii"),
            b"Il presente verbale",
            b"Partecipazione e rappresentanze",
            b"Sottoscrizione del verbale",
            b"Roma, 23/11/2026",
            b"Firma del presidente",
            b"Appendice A",
        )
        positions = [pdf.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))

        page = self.client.get(self.form_url)
        self.assertContains(page, 'type="date"')
        self.assertContains(page, 'value="2026-11-23"')
        self.assertEqual(page.context["form"]["agenda"].value(), agenda)
        self.assertEqual(page.context["form"]["discussion"].value(), discussion)
        self.election.refresh_from_db()
        self.assertEqual(self.election.minutes_signing_city, "Roma")
        self.assertEqual(self.election.minutes_signing_date, date(2026, 11, 23))

    def test_optional_sections_can_be_cleared_independently(self):
        self.set_report_data()
        self.election.minutes_agenda = "Punto dimostrativo"
        self.election.minutes_discussion = "Discussione dimostrativa"
        self.election.save(update_fields=["minutes_agenda", "minutes_discussion"])
        with patch("reportlab.rl_config.pageCompression", 0):
            response = self.client.post(
                self.form_url, {"download": "pdf", "agenda": "  \r\n  "}
            )
            pdf = b"".join(response.streaming_content)
            self.assertNotIn(b"Ordine del giorno", pdf)
            self.assertIn(b"Discussione dimostrativa", pdf)
            response = self.client.post(
                self.form_url,
                {"download": "pdf", "agenda": "Punto aggiornato", "discussion": ""},
            )
            pdf = b"".join(response.streaming_content)
            self.assertIn(b"Punto aggiornato", pdf)
            self.assertNotIn(b"(Discussione)", pdf)

    def test_minutes_preview_uses_unsaved_fields_without_recording_changes(self):
        self.set_report_data()
        self.election.minutes_discussion = "Discussione salvata"
        self.election.save(update_fields=["minutes_discussion"])
        fields = {
            "download": "preview",
            "agenda": "Punto in anteprima",
            "discussion": "Discussione in anteprima",
            "president": "Ada Demo",
            "secretary": "Elena Prova",
            "signing_city": "Roma",
            "signing_date": "2026-11-23",
        }
        self.assertContains(self.client.get(self.form_url), "Anteprima verbale")
        self.assertEqual(Client().post(self.form_url, fields).status_code, 302)
        with patch("reportlab.rl_config.pageCompression", 0):
            response = self.client.post(self.form_url, fields)
            pdf = b"".join(response.streaming_content)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            response["Content-Disposition"],
            f'inline; filename="Anteprima_{assembly_report.PDF_FILENAME}"',
        )
        for text in (
            b"Punto in anteprima",
            b"Discussione in anteprima",
            b"Ada Demo",
            b"Elena Prova",
            b"Roma, 23/11/2026",
        ):
            self.assertIn(text, pdf)
        self.election.refresh_from_db()
        self.assertEqual(self.election.minutes_discussion, "Discussione salvata")
        self.assertEqual(self.election.minutes_agenda, "")
        self.assertEqual(self.election.minutes_president, "")
        self.assertIsNone(self.election.minutes_signing_date)
        self.assertFalse(
            AuditEvent.objects.filter(
                election=self.election, action="dati_verbale_aggiornati"
            ).exists()
        )

    def test_signer_name_over_the_limit_returns_the_form_with_an_error(self):
        self.set_report_data()
        self.election.minutes_secretary = "Segretaria Demo"
        self.election.save(update_fields=["minutes_secretary"])
        response = self.client.post(
            self.form_url,
            {"download": "pdf", "president": "A" * 181, "secretary": "Elena Prova"},
        )
        self.assertIn("president", response.context["form"].errors)
        self.assertContains(response, 'value="Elena Prova"')
        self.election.refresh_from_db()
        self.assertEqual(self.election.minutes_secretary, "Segretaria Demo")

    def test_word_export_contains_editable_minutes_and_saves_current_form(self):
        self.set_report_data()
        self.assertContains(self.client.get(self.form_url), "Scarica verbale Word")
        fields = {
            "download": "docx",
            "agenda": "Punto dimostrativo",
            "discussion": "Testo della discussione",
            "president": "Ada Demo",
            "secretary": "Elena Prova",
            "signing_city": "Roma",
            "signing_date": "2026-11-23",
        }
        self.assertEqual(Client().post(self.form_url, fields).status_code, 302)
        response = self.client.post(self.form_url, fields)
        self.assertEqual(response["Content-Type"], assembly_report.WORD_MIMETYPE)
        self.assertIn(assembly_report.WORD_FILENAME, response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        document = Document(BytesIO(b"".join(response.streaming_content)))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        titles = (
            "Ordine del giorno",
            "Discussione",
            "Partecipazione e rappresentanze",
            "Esito delle votazioni",
            "Impronte SHA-256",
            "Sottoscrizione del verbale",
            "Roma, 23/11/2026",
            "Appendice A - Aventi diritto al voto",
            "Appendice B - Votanti e scelte per quesito",
        )
        positions = [paragraphs.index(title) for title in titles]
        self.assertEqual(positions, sorted(positions))
        discussion = next(
            item for item in document.paragraphs if item.text == fields["discussion"]
        )
        self.assertEqual(discussion.paragraph_format.space_after, Pt(36))
        table_text = [
            cell.text
            for table in document.tables
            for row in table.rows
            for cell in row.cells
        ]
        self.assertIn("Firma del presidente\nAda Demo\n________________________________", table_text)
        self.assertIn("Firma del segretario\nElena Prova\n________________________________", table_text)
        self.assertIn("TEST-7", table_text)
        self.assertTrue(any("Delega esercitata da" in cell for cell in table_text))
        discussion.text = "Discussione corretta"
        edited = BytesIO()
        document.save(edited)
        edited.seek(0)
        self.assertIn("Discussione corretta", [p.text for p in Document(edited).paragraphs])
        self.election.refresh_from_db()
        self.assertEqual(self.election.minutes_discussion, fields["discussion"])

    def test_secret_minutes_include_the_full_roll_without_a_named_ballot_appendix(self):
        self.election.mode = Election.Mode.SECRET
        self.election.save(update_fields=["mode"])
        open_election(self.election, add_members(2))
        Election.objects.filter(pk=self.election.pk).update(
            status=Election.Status.TALLIED, ended_at=timezone.now()
        )
        with patch("reportlab.rl_config.pageCompression", 0):
            response = self.client.get(self.url("pdf"))
            pdf = b"".join(response.streaming_content)
        self.assertIn(b"Appendice A - Aventi diritto al voto", pdf)
        self.assertIn(b"TEST-0", pdf)
        self.assertIn(b"TEST-1", pdf)
        self.assertNotIn(b"Appendice B", pdf)
        self.assertNotIn(b"Scelte registrate", pdf)
        response = self.client.get(self.url("docx"))
        document = Document(BytesIO(b"".join(response.streaming_content)))
        headings = [item.text for item in document.paragraphs]
        self.assertIn("Appendice A - Aventi diritto al voto", headings)
        self.assertNotIn("Appendice B - Votanti e scelte per quesito", headings)
        self.assertEqual(document.tables[-1].cell(0, 0).text, "Matricola")
