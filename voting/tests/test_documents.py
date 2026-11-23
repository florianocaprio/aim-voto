from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from voting.forms import DocumentForm

from .support import election_with_motion, staff


@override_settings(REQUIRE_ADMIN_MFA=False, FILE_UPLOAD_MAX_MEMORY_SIZE=1024)
class DocumentUploadTests(TestCase):
    def setUp(self):
        actor = staff()
        self.client.force_login(actor)
        self.election, *_ = election_with_motion(actor)
        self.page = reverse("voting:documents", args=[self.election.pk])

    def upload(self, name, data):
        return self.client.post(
            self.page, {"file": SimpleUploadedFile(name, data, "application/pdf")}
        )

    def test_consecutive_upload_and_removal_stay_on_documents_page(self):
        documents = {
            "Nota dimostrativa.pdf": b"%PDF-1.4\n",
            "Allegato dimostrativo.pdf": b"%PDF-1.4\n" + b" " * 2048,
        }
        for name, data in documents.items():
            self.assertRedirects(self.upload(name, data), self.page)
            self.assertContains(self.client.get(self.page), name)
            document = self.election.documents.get(name=name)
            url = reverse("voting:admin_document", args=[self.election.pk, document.pk])
            self.assertEqual(b"".join(self.client.get(url).streaming_content), data)
        self.assertEqual(self.election.documents.count(), 2)
        selected = self.election.documents.get(name="Nota dimostrativa.pdf")
        self.assertRedirects(
            self.client.post(self.page, {"remove": selected.pk}), self.page
        )
        page = self.client.get(self.page)
        self.assertNotContains(page, selected.name)
        self.assertContains(page, "Allegato dimostrativo.pdf")
        self.assertContains(page, "Carica PDF")

    def test_unreadable_file_is_a_form_error(self):
        upload = SimpleUploadedFile("Nota dimostrativa.pdf", b"%PDF-1.4\n")
        upload.close()
        form = DocumentForm({}, {"file": upload})
        self.assertFalse(form.is_valid())
        self.assertIn("Seleziona di nuovo il file", form.errors["file"][0])
        self.assertFalse(self.election.documents.exists())
