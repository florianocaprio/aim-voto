from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from members.models import Member
from voting.models import Delegation, ElectionVoter, NamedBallot

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

    def test_electorate_order_applies_to_filtered_results_before_pagination(self):
        voters = ElectionVoter.objects.bulk_create(
            [
                ElectionVoter(
                    election=self.election,
                    first_name=f"Socio {index:02d}",
                    last_name=f"Fittizio {40 - index:02d}",
                    external_id=f"TEST-{index:02d}",
                )
                for index in range(41)
            ]
        )
        self.election.voters.create(**self.data)
        for sort, expected in (
            ("name", voters),
            ("name-desc", voters[::-1]),
            ("surname", voters[::-1]),
            ("surname-desc", voters),
            ("unsupported", voters[::-1]),
        ):
            with self.subTest(sort=sort):
                response = self.client.get(
                    self.list_url, {"q": "Fittizio", "sort": sort}
                )
                self.assertEqual(list(response.context["page"]), expected[:40])
                selected = response.context["sort"]
                self.assertContains(
                    response, f'href="?q=Fittizio&amp;sort={selected}&amp;page=2"'
                )
                second_page = self.client.get(
                    self.list_url, {"q": "Fittizio", "sort": selected, "page": 2}
                )
                self.assertEqual(list(second_page.context["page"]), expected[40:])
                self.assertContains(
                    second_page, f'href="?q=Fittizio&amp;sort={selected}&amp;page=1"'
                )

    def test_electorate_orders_and_outlines_participation_states(self):
        delegate, absent, submitted, delegated, direct, represented = [
            self.election.voters.create(
                first_name=name, last_name="Fittizio", external_id=f"TEST-{index}"
            )
            for index, name in enumerate(
                (
                    "Delegato",
                    "Assente",
                    "Richiesta",
                    "Delegante",
                    "Presente",
                    "Rappresentato",
                )
            )
        ]
        self.election.allow_delegations = True
        self.election.save(update_fields=["allow_delegations"])
        self.assertEqual(
            self.client.post(
                reverse("voting:transition", args=[self.election.pk, "apri"]),
                {"confirm": "yes"},
            ).status_code,
            302,
        )
        for grantor, holder, state in (
            (submitted, direct, Delegation.State.PENDING),
            (delegated, delegate, Delegation.State.APPROVED),
            (represented, direct, Delegation.State.APPROVED),
        ):
            Delegation.objects.create(
                grantor=grantor,
                delegate=holder,
                state=state,
                requested_at=timezone.now(),
            )
        NamedBallot.objects.create(voter=delegate)
        NamedBallot.objects.create(voter=direct)
        NamedBallot.objects.create(voter=represented, cast_by=direct)

        pending_first = self.client.get(self.list_url, {"sort": "to-finish"})
        expected = [delegate, absent, submitted, delegated, direct, represented]
        self.assertEqual(list(pending_first.context["page"]), expected)
        done_first = self.client.get(self.list_url, {"sort": "finished"})
        self.assertEqual(list(done_first.context["page"]), expected[::-1])
        for badge, label, count in (
            (" vote-status-voted", "Ha votato", 2),
            (" vote-status-voted", "Ha votato per delega", 1),
            (" vote-status-unvoted", "Non ha ancora votato", 1),
            ("", "Delega inviata · in verifica", 1),
            ("", "Delega conferita", 1),
        ):
            self.assertContains(
                pending_first,
                f'<span class="vote-status{badge}">{label}</span>',
                html=True,
                count=count,
            )
        self.assertContains(pending_first, "1 delega da votare", count=1)
