from io import StringIO
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from mailings.models import CampaignState, MailKind, ReminderCampaign
from voting.models import AuditEvent, ElectionAdministrator

from .support import add_members, election_with_motion, open_election, staff


@override_settings(REQUIRE_ADMIN_MFA=False)
class AdministrationAccessTests(TestCase):
    def setUp(self):
        self.superadmin = staff(superadmin=True)
        self.client.force_login(self.superadmin)
        self.assigned, *_ = election_with_motion(self.superadmin, "Assemblea assegnata")
        self.other, *_ = election_with_motion(self.superadmin, "Assemblea riservata")
        users = get_user_model().objects
        self.global_admin = users.create(username="globale", is_staff=True)
        self.limited_admin = users.create(username="assegnato", is_staff=True)
        self.assignment = ElectionAdministrator.objects.create(user=self.limited_admin)
        self.assignment.elections.add(self.assigned)

    def client_of(self, account):
        session = Client()
        session.force_login(account)
        return session

    def test_superadmin_creates_profiles_and_changes_scope(self):
        index = reverse("voting:administrators")
        create = reverse("voting:administrator_create")
        self.assertContains(self.client.get(index), "Nuovo amministratore")
        password = "v3!SyntheticPass_demo#80"
        for scope, username, elections in (
            ("global", "nuovo-globale", []),
            ("election", "nuovo-assegnato", [self.assigned.pk, self.other.pk]),
        ):
            with self.subTest(scope=scope):
                response = self.client.post(
                    create,
                    {
                        "username": username,
                        "password1": password,
                        "password2": password,
                        "scope": scope,
                        "elections": elections,
                        "is_superuser": True,
                    },
                )
                self.assertRedirects(response, index)
                account = get_user_model().objects.get(username=username)
                self.assertTrue(account.is_staff and account.check_password(password))
                self.assertFalse(account.is_superuser)
                if elections:
                    self.assertCountEqual(
                        account.election_administration.elections.values_list(
                            "pk", flat=True
                        ),
                        elections,
                    )
                else:
                    self.assertFalse(hasattr(account, "election_administration"))
        self.assertEqual(
            AuditEvent.objects.filter(action="amministratore_creato").count(), 2
        )
        invalid = self.client.post(
            create,
            {
                "username": "senza-votazione",
                "password1": password,
                "password2": password,
                "scope": "election",
            },
        )
        self.assertContains(invalid, "Seleziona almeno una consultazione da assegnare.")
        self.assertFalse(
            get_user_model().objects.filter(username="senza-votazione").exists()
        )

        limited = self.client_of(self.limited_admin)
        edit = reverse("voting:administrator_edit", args=[self.limited_admin.pk])
        self.assertEqual(
            self.client.get(edit).context["scope_form"].initial["elections"],
            [self.assigned.pk],
        )
        self.assertRedirects(
            self.client.post(
                edit,
                {"scope": "election", "elections": [self.assigned.pk, self.other.pk]},
            ),
            index,
        )
        dashboard = limited.get(reverse("voting:dashboard"))
        for election in (self.assigned, self.other):
            self.assertContains(dashboard, election.title)
            self.assertEqual(
                limited.get(reverse("voting:detail", args=[election.pk])).status_code,
                200,
            )
        invalid = self.client.post(
            edit,
            {"scope": "election", "elections": [self.other.pk, self.other.pk + 100]},
        )
        self.assertTrue(invalid.context["scope_form"].errors["elections"])
        self.assertCountEqual(
            self.assignment.elections.all(), [self.assigned, self.other]
        )
        self.assertRedirects(
            self.client.post(edit, {"scope": "election", "elections": [self.other.pk]}),
            index,
        )
        self.assertEqual(
            limited.get(reverse("voting:detail", args=[self.assigned.pk])).status_code,
            404,
        )
        self.assertRedirects(self.client.post(edit, {"scope": "election"}), index)
        self.assertTrue(
            ElectionAdministrator.objects.filter(user=self.limited_admin).exists()
        )
        self.assertContains(
            limited.get(reverse("voting:dashboard")), "Nessuna consultazione assegnata."
        )
        self.assertEqual(
            self.client.get(
                reverse("voting:administrator_edit", args=[self.superadmin.pk])
            ).status_code,
            404,
        )

    def test_scoped_access_is_enforced_across_voting_import_and_mail_history(self):
        limited = self.client_of(self.limited_admin)
        global_staff = self.client_of(self.global_admin)
        dashboard = reverse("voting:dashboard")
        assigned, other = self.assigned.pk, self.other.pk
        second, *_ = election_with_motion(
            self.superadmin, "Seconda assemblea assegnata"
        )
        self.assignment.elections.add(second)
        self.assertContains(limited.get(dashboard), self.assigned.title)
        self.assertContains(limited.get(dashboard), second.title)
        self.assertNotContains(limited.get(dashboard), self.other.title)
        self.assertNotContains(limited.get(dashboard), "Nuova consultazione")
        self.assertContains(global_staff.get(dashboard), self.other.title)
        self.assertContains(global_staff.get(dashboard), "Nuova consultazione")
        self.assertEqual(limited.post(reverse("voting:create")).status_code, 404)
        for name in (
            "voting:detail",
            "voting:voters",
            "voting:edit",
            "members:import",
            "members:template-csv",
            "mailings:history",
        ):
            with self.subTest(view=name):
                for allowed in (assigned, second.pk):
                    self.assertEqual(
                        limited.get(reverse(name, args=[allowed])).status_code, 200
                    )
                self.assertEqual(
                    limited.get(reverse(name, args=[other])).status_code, 404
                )
        for name, args in (
            ("voting:transition", [other, "elimina"]),
            ("voting:question_add", [other]),
            ("voting:staff_delegation", [other, 10, "approva"]),
            ("mailings:cancel", [other, uuid4()]),
            ("members:preview", [other, uuid4()]),
        ):
            with self.subTest(view=name):
                self.assertEqual(
                    limited.post(
                        reverse(name, args=args), {"confirm": "yes"}
                    ).status_code,
                    404,
                )
        self.assertTrue(type(self.other).objects.filter(pk=other).exists())
        self.assigned.delete()
        self.assertContains(limited.get(dashboard), second.title)
        second.delete()
        self.assertContains(limited.get(dashboard), "Nessuna consultazione assegnata.")
        self.assertEqual(
            limited.get(reverse("voting:detail", args=[other])).status_code, 404
        )

    def test_links_and_admin_menu_are_reserved_to_superadmin(self):
        (voter,) = open_election(self.assigned, add_members(1))
        voter.phone = "+393400000001"
        voter.save(update_fields=["phone"])
        election_id = self.assigned.pk
        election = [election_id]
        position = [election_id, voter.pk]
        urls = {
            "copy": reverse("voting:copy_link", args=position),
            "detail": reverse("voting:voter_detail", args=position),
        }
        preview = ReminderCampaign.objects.create(
            election=self.assigned,
            author=self.global_admin,
            kind=MailKind.INVITE,
            state=CampaignState.PREVIEW,
        )
        for account in (self.global_admin, self.limited_admin):
            with self.subTest(account=account.username):
                session = self.client_of(account)
                self.assertNotContains(
                    session.get(reverse("voting:dashboard")),
                    "/gestione/amministratori/",
                )
                self.assertNotContains(
                    session.get(urls["detail"]), "Copia link personale"
                )
                self.assertNotContains(
                    session.get(reverse("voting:voters", args=election)), urls["copy"]
                )
                for name, args in (
                    ("voting:administrator_create", []),
                    ("voting:administrator_edit", [account.pk]),
                    ("voting:administrators", []),
                    ("voting:contact", position),
                    ("voting:rotate", position),
                    ("mailings:invite", election),
                    ("mailings:remind", election),
                    ("mailings:resend", position),
                    ("mailings:confirm", [election_id, preview.pk]),
                ):
                    self.assertEqual(
                        session.post(reverse(name, args=args)).status_code, 403
                    )
                for payload in ({}, {"whatsapp_phone": "393400000001"}):
                    self.assertEqual(
                        session.post(urls["copy"], payload).status_code, 403
                    )
        self.assertFalse(AuditEvent.objects.filter(action="copia_link").exists())
        self.assertContains(self.client.get(urls["detail"]), "Copia link personale")
        copied = self.client.post(
            urls["copy"], {"whatsapp_phone": "393400000001"}
        ).json()
        self.assertIn("/accedi/#", copied["url"])
        self.assertIn("web.whatsapp.com", copied["whatsapp_url"])
        self.assertEqual(
            self.client.get(
                reverse("mailings:campaign", args=[election_id, preview.pk])
            ).status_code,
            200,
        )

    def test_rename_promotes_in_place_and_preserves_password_and_second_factor(self):
        old_name, new_name = "responsabile-demo", "superadmin"
        users = get_user_model().objects
        account = users.create_user(
            old_name, password="demo-Synthet1c#Pass", is_staff=True
        )
        device = TOTPDevice.objects.create(user=account, name="Test TOTP")
        password_hash = account.password
        conflicting = users.create(username=new_name)
        with self.assertRaises(CommandError):
            call_command("promote_staff", old_name, rename=new_name, stdout=StringIO())
        account.refresh_from_db()
        self.assertEqual(account.username, old_name)
        self.assertFalse(account.is_superuser)
        conflicting.delete()
        for _ in range(2):
            call_command("promote_staff", old_name, rename=new_name, stdout=StringIO())
        account.refresh_from_db()
        self.assertEqual(
            (account.username, account.password), (new_name, password_hash)
        )
        self.assertTrue(account.is_superuser)
        self.assertEqual(TOTPDevice.objects.get(pk=device.pk).user_id, account.pk)
        self.assertFalse(users.filter(username=old_name).exists())
        self.assertEqual(
            AuditEvent.objects.filter(action="account_promosso_superadmin").count(), 1
        )
