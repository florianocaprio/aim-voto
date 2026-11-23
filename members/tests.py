from datetime import timedelta
from io import BytesIO, StringIO
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from voting.models import AuditEvent, Election

from .demo import DEMO_MEMBERS
from .importing import IMPORT_FIELDS, ImportFormatError, read_upload
from .models import ImportBatch, Member

CSV_HEADER = "ID socio;Nome;Cognome;Email;Cellulare;Sezione\n"
MAPPING = {name: str(i) for i, name in enumerate(IMPORT_FIELDS)}


class ParseTests(SimpleTestCase):
    def test_csv_encoding_and_header_flag(self):
        upload = SimpleUploadedFile(
            "dati.csv", "0001,  André  ,Riva,a@example.invalid,,Nord\n".encode("cp1252")
        )
        headings, records, blank = read_upload(upload, "cp1252", ",", False)
        self.assertEqual(headings, [""] * 6)
        self.assertEqual(records[0]["cells"][:2], ["0001", "André"])
        self.assertEqual(blank, 0)

    def test_xlsx_formula_is_reported_and_numeric_cells_are_identified(self):
        workbook = Workbook()
        workbook.active.append(list(IMPORT_FIELDS.values()))
        workbook.active.append(
            [1, "=2+2", "Bianchi", "a@example.invalid", None, "Nord"]
        )
        data = BytesIO()
        workbook.save(data)
        upload = SimpleUploadedFile("dati.xlsx", data.getvalue())
        headings, records, blank = read_upload(upload, "utf-8-sig", ";", True)
        self.assertEqual(headings, list(IMPORT_FIELDS.values()))
        self.assertEqual(records[0]["errors"], ["La colonna 2 contiene una formula."])
        self.assertEqual(records[0]["non_text"], [0])
        self.assertNotIn("=2+2", records[0]["cells"])
        self.assertEqual(blank, 0)

    def test_macro_parts_are_rejected(self):
        data = BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("xl/vbaProject.bin", b"demo")
        upload = SimpleUploadedFile("dati.xlsx", data.getvalue())
        with self.assertRaisesMessage(ImportFormatError, "macro"):
            read_upload(upload, "utf-8-sig", ";", True)


@override_settings(ALLOWED_HOSTS=["testserver"], REQUIRE_ADMIN_MFA=False)
class ImportFlowTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_user(
            username="operatore", is_staff=True
        )
        self.client.force_login(self.operator)
        self.election = Election.objects.create(
            title="Consultazione di prova",
            created_by=self.operator,
            closes_at=timezone.now() + timedelta(days=2),
        )

    def stage(self, content, mapping=None, filename="dati.csv"):
        upload = self.client.post(
            reverse("members:import", args=[self.election.pk]),
            {
                "file": SimpleUploadedFile(filename, content),
                "encoding": "utf-8-sig",
                "separator": ";",
                "has_header": "on",
            },
        )
        self.assertEqual(upload.status_code, 302, upload.content.decode()[:150])
        associate = self.client.post(
            upload["Location"], MAPPING if mapping is None else mapping
        )
        self.assertEqual(associate.status_code, 302, associate.content.decode()[:150])
        return associate["Location"]

    def post_confirmation(self, url, preview):
        return self.client.post(
            url,
            {"fingerprint": preview.context["preview"].fingerprint, "confirmed": "on"},
        )

    def test_import_and_repeat_update_keep_voter_id(self):
        url = self.stage(
            (
                CSV_HEADER
                + "0001;Ada;Bianchi;prima@example.invalid;+393330000001;Nord\n"
            ).encode()
        )
        preview = self.client.get(url)
        self.assertEqual(preview.context["summary"]["new"], 1)
        self.assertEqual(self.election.voters.count(), 0)
        receipt = self.post_confirmation(url, preview)
        self.assertRedirects(receipt, url)
        member = self.election.voters.get(external_id="0001")
        self.assertEqual(member.email, "prima@example.invalid")
        self.assertEqual(ImportBatch.objects.get().records, [])
        event = AuditEvent.objects.get(action="importazione_elettori")
        self.assertEqual(event.actor, self.operator)
        self.assertEqual(event.election, self.election)
        self.assertEqual(event.details["new"], 1)
        retry = self.post_confirmation(url, preview)
        self.assertContains(retry, "Importazione completata")
        self.assertEqual(
            AuditEvent.objects.filter(action="importazione_elettori").count(), 1
        )

        update = {
            "external_id": "0",
            "first_name": "1",
            "last_name": "2",
            "email": "3",
            "phone": "",
            "section": "4",
        }
        data = b"ID socio;Nome;Cognome;Email;Sezione\n0001;Ada;Bianchi;seconda@example.invalid;Centro\n"
        new_url = self.stage(data, update)
        new_preview = self.client.get(new_url)
        self.assertEqual(new_preview.context["summary"]["updated"], 1)
        self.assertRedirects(self.post_confirmation(new_url, new_preview), new_url)
        self.assertEqual(self.election.voters.count(), 1)
        member.refresh_from_db()
        self.assertEqual(member.email, "seconda@example.invalid")
        self.assertEqual(member.phone, "+393330000001")
        self.assertEqual(
            self.client.get(self.stage(data, update)).context["summary"]["unchanged"], 1
        )

    def test_invalid_rows_block_save_and_id_repetition_is_visible(self):
        content = CSV_HEADER + (
            "X;Ada;Bianchi;ada@example.invalid;;Nord\n"
            "X;Clara;Rossi;clara@example.invalid;;Nord\n"
            "Y;;Verdi;errata;;Centro\n"
        )
        url = self.stage(content.encode())
        preview = self.client.get(url)
        self.assertEqual(preview.context["summary"]["errors"], 3)
        self.assertContains(preview, "Matricola ripetuta", count=2)
        self.assertTrue(
            any(
                error.startswith("Email.")
                for error in preview.context["preview"].rows[2].errors
            )
        )
        self.assertNotContains(preview, "Conferma importazione")
        self.assertEqual(self.post_confirmation(url, preview).status_code, 409)
        self.assertFalse(self.election.voters.exists())

    def test_shared_addresses_remain_distinct_with_warnings(self):
        content = CSV_HEADER + (
            "X;Ada;Bianchi;gruppo@example.invalid;;Nord\n"
            "Y;Clara;Rossi;Gruppo@example.invalid;;Centro\n"
        )
        url = self.stage(content.encode())
        preview = self.client.get(url)
        self.assertContains(preview, "Email condivisa", count=2)
        self.assertEqual(preview.context["summary"]["warnings"], 2)
        self.assertRedirects(self.post_confirmation(url, preview), url)
        self.assertEqual(self.election.voters.count(), 2)

    def test_changes_between_preview_and_confirm_require_new_confirmation(self):
        content = CSV_HEADER + "X;Ada;Bianchi;ada@example.invalid;;Nord\n"
        url = self.stage(content.encode())
        preview = self.client.get(url)
        self.election.voters.create(
            **{**DEMO_MEMBERS[0], "email": "ada@example.invalid"}
        )
        stale = self.post_confirmation(url, preview)
        self.assertContains(stale, "L’anteprima è cambiata", status_code=409)
        self.assertContains(stale, "Email condivisa", status_code=409)
        self.assertFalse(self.election.voters.filter(external_id="X").exists())
        self.assertRedirects(self.post_confirmation(url, stale), url)
        self.assertTrue(self.election.voters.filter(external_id="X").exists())

    def test_confirmation_and_batch_content_are_scoped_to_the_upload_owner(self):
        url = self.stage(
            (CSV_HEADER + "X;Ada;Bianchi;ada@example.invalid;;Nord\n").encode()
        )
        preview = self.client.get(url)
        self.assertIn("no-store", preview["Cache-Control"])
        another = get_user_model().objects.create_user(
            username="altro-operatore", is_staff=True
        )
        self.client.force_login(another)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.post_confirmation(url, preview).status_code, 404)
        regular = get_user_model().objects.create_user(username="elettore")
        self.client.force_login(regular)
        self.assertEqual(
            self.client.get(
                reverse("members:import", args=[self.election.pk])
            ).status_code,
            403,
        )

    def test_templates_and_demo_import(self):
        response = self.client.get(
            reverse("members:template-xlsx", args=[self.election.pk])
        )
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content), read_only=True)
        self.assertEqual(next(workbook.active.values), tuple(IMPORT_FIELDS.values()))
        workbook.close()
        csv = self.client.get(
            reverse("members:demo-csv", args=[self.election.pk])
        ).content
        url = self.stage(csv)
        preview = self.client.get(url)
        self.assertEqual(preview.context["summary"]["new"], 3)
        self.assertRedirects(self.post_confirmation(url, preview), url)
        self.assertEqual(self.election.voters.count(), 3)
        self.assertTrue(
            all(
                member.email.endswith("@example.invalid") and not member.phone
                for member in self.election.voters.all()
            )
        )
        call_command("seed_demo", self.election.pk, stdout=StringIO())
        self.assertEqual(self.election.voters.count(), 3)

    def test_xlsx_import_uses_selected_columns_and_requires_checkbox(self):
        workbook = Workbook()
        workbook.active.append(
            ["Identificativo", "Indirizzo", "Sezione", "Nome", "Cognome"]
        )
        workbook.active.append(
            ["0002", "ada@example.invalid", "Nord", "Ada", "Bianchi"]
        )
        data = BytesIO()
        workbook.save(data)
        mapping = {
            "external_id": "0",
            "email": "1",
            "section": "2",
            "first_name": "3",
            "last_name": "4",
            "phone": "",
        }
        url = self.stage(data.getvalue(), mapping, filename="dati.xlsx")
        preview = self.client.get(url)
        self.assertEqual(preview.context["summary"]["new"], 1)
        self.assertEqual(
            self.client.post(
                url, {"fingerprint": preview.context["preview"].fingerprint}
            ).status_code,
            400,
        )
        self.assertFalse(self.election.voters.exists())
        self.assertRedirects(self.post_confirmation(url, preview), url)
        self.assertEqual(self.election.voters.get().external_id, "0002")

    def test_expired_upload_is_inaccessible_and_can_be_purged(self):
        url = self.stage(
            (CSV_HEADER + "X;Ada;Bianchi;ada@example.invalid;;Nord\n").encode()
        )
        ImportBatch.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(url).status_code, 404)
        call_command("purge_imports", stdout=StringIO())
        self.assertFalse(ImportBatch.objects.exists())

    def test_csv_with_only_four_required_columns_is_scoped_to_the_consultation(self):
        historical = Election.objects.create(
            title="Consultazione conclusa",
            created_by=self.operator,
            closes_at=timezone.now(),
            status=Election.Status.TALLIED,
        )
        identity = {
            "external_id": "0007",
            "first_name": "Ada",
            "last_name": "Riva",
            "email": "precedente@example.invalid",
            "section": "Nord",
        }
        voter = historical.voters.create(
            member=Member.objects.create(**identity), **identity
        )
        upload = self.stage(
            b"Nome;Cognome;Matricola;Email\nAda;Riva;0007;nuova@example.invalid\n",
            {"first_name": "0", "last_name": "1", "external_id": "2", "email": "3"},
        )
        preview = self.client.get(upload)
        self.assertEqual(preview.context["summary"]["new"], 1)
        other_draft = Election.objects.create(
            title="Altra bozza",
            created_by=self.operator,
            closes_at=timezone.now() + timedelta(days=2),
        )
        cross_scope = reverse(
            "members:preview", args=[other_draft.pk, preview.context["batch"].pk]
        )
        self.assertEqual(self.client.get(cross_scope).status_code, 404)
        self.assertEqual(self.post_confirmation(cross_scope, preview).status_code, 404)
        self.assertRedirects(self.post_confirmation(upload, preview), upload)
        new_voter = self.election.voters.get(external_id="0007")
        self.assertEqual(new_voter.email, "nuova@example.invalid")
        self.assertIsNone(new_voter.member_id)
        self.assertEqual(new_voter.section, "")
        self.assertEqual(new_voter.phone, "")
        voter.refresh_from_db()
        self.assertEqual(voter.email, "precedente@example.invalid")
        self.assertEqual(voter.section, "Nord")
        self.assertEqual(voter.member.email, "precedente@example.invalid")

    def test_pending_import_cannot_change_open_electorate(self):
        url = self.stage(
            (CSV_HEADER + "X;Ada;Bianchi;ada@example.invalid;;Nord\n").encode()
        )
        preview = self.client.get(url)
        Election.objects.filter(pk=self.election.pk).update(status=Election.Status.OPEN)
        self.assertEqual(self.post_confirmation(url, preview).status_code, 404)
        self.assertFalse(self.election.voters.exists())
