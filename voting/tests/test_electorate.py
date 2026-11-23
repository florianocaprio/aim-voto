from django.test import TestCase, override_settings
from django.urls import reverse

from members.models import Member

from .support import election_with_motion, staff


@override_settings(REQUIRE_ADMIN_MFA=False)
class ElectorateTests(TestCase):
    def setUp(self):
        self.actor = staff()
        self.client.force_login(self.actor)
        self.election, _, _ = election_with_motion(self.actor)
        self.add_url = reverse("voting:voter_add", args=[self.election.pk])
        self.list_url = reverse("voting:voters", args=[self.election.pk])
        self.data = {
            "first_name": "Ada",
            "last_name": "Riva",
            "external_id": "0001",
            "email": "ada@example.invalid",
        }

    def test_manual_roster_accepts_same_number_in_two_distinct_consultations(self):
        index = self.client.get(self.list_url)
        self.assertContains(index, "Aggiungi elettore")
        self.assertContains(index, "Importa CSV")
        self.assertEqual(self.client.get("/gestione/soci/").status_code, 404)
        self.assertRedirects(self.client.post(self.add_url, self.data), self.list_url)
        self.assertContains(
            self.client.post(self.add_url, self.data), "matricola è già presente"
        )
        another, _, _ = election_with_motion(self.actor, title="Seconda consultazione")
        new_record = {**self.data, "email": "recapito-nuovo@example.invalid"}
        self.assertEqual(
            self.client.post(
                reverse("voting:voter_add", args=[another.pk]), new_record
            ).status_code,
            302,
        )
        self.assertEqual(self.election.voters.get().email, "ada@example.invalid")
        self.assertEqual(another.voters.get().email, new_record["email"])
        self.assertFalse(Member.objects.exists())

    def test_draft_edit_remove_and_freeze_on_open(self):
        self.assertEqual(self.client.post(self.add_url, self.data).status_code, 302)
        voter = self.election.voters.get()
        edit_url = reverse("voting:voter_edit", args=[self.election.pk, voter.pk])
        remove_url = reverse("voting:voter_remove", args=[self.election.pk, voter.pk])
        self.assertRedirects(
            self.client.post(edit_url, {**self.data, "last_name": "Bianchi"}),
            self.list_url,
        )
        self.assertEqual(self.client.post(remove_url).status_code, 400)
        voter.refresh_from_db()
        self.assertEqual(voter.last_name, "Bianchi")
        second = self.election.voters.create(**{**self.data, "external_id": "0002"})
        remove_second = reverse(
            "voting:voter_remove", args=[self.election.pk, second.pk]
        )
        self.assertRedirects(
            self.client.post(remove_second, {"confirm": "yes"}), self.list_url
        )
        self.assertEqual(self.election.voters.count(), 1)
        self.assertEqual(
            self.client.post(
                reverse("voting:transition", args=[self.election.pk, "apri"]),
                {"confirm": "yes"},
            ).status_code,
            302,
        )
        self.assertNotContains(self.client.get(self.list_url), "Aggiungi elettore")
        self.assertEqual(self.client.post(self.add_url, self.data).status_code, 404)
        self.assertEqual(self.client.post(edit_url, self.data).status_code, 404)
        self.assertEqual(
            self.client.post(remove_url, {"confirm": "yes"}).status_code, 404
        )
        self.assertEqual(self.election.voters.get().last_name, "Bianchi")
        self.assertTrue(hasattr(self.election.voters.get(), "credential"))
