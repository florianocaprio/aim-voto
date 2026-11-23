from io import StringIO
from secrets import token_urlsafe

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from members.models import Member
from voting.models import Election, ElectionVoter, VotingCredential
from voting.services import credential_fields, personal_url


class PrivacyHeadersTests(SimpleTestCase):
    def test_oversized_post_rejected_without_reading_body(self):
        result = self.client.generic(
            "POST", reverse("staff_login"), b"", CONTENT_LENGTH=str(settings.MAX_REQUEST_BYTES + 1)
        )
        self.assertEqual(result.status_code, 413)
        self.assertIn("no-store", result["Cache-Control"])
        self.assertEqual(result["Referrer-Policy"], "same-origin")


@override_settings(REQUIRE_ADMIN_MFA=True)
class StaffMfaTests(TestCase):
    def test_password_requires_totp_and_verified_session_opens_dashboard(self):
        password = token_urlsafe(24)
        user = get_user_model().objects.create_user("operatore-demo", password=password, is_staff=True)
        device = TOTPDevice.objects.create(user=user, name="Test")
        dashboard = reverse("voting:dashboard")
        challenge = f"{reverse('staff_otp')}?next=/gestione/"
        result = self.client.post(reverse("staff_login"), {"username": user.username, "password": password})
        self.assertRedirects(result, reverse("staff_otp") + "?next=%2Fgestione%2F")
        self.assertRedirects(self.client.get(dashboard), challenge)
        result = self.client.post(
            reverse("staff_otp"),
            {"token": f"{totp(device.bin_key):06d}", "next": dashboard},
        )
        self.assertRedirects(result, dashboard)
        device.delete()
        self.assertRedirects(self.client.get(dashboard), challenge)

    def test_regular_user_denied_even_if_authenticated(self):
        user = get_user_model().objects.create_user("socio-demo")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("voting:dashboard")).status_code, 403)


class EncryptionRotationTests(TestCase):
    def test_reencrypt_preserves_url_digest_and_elector_credential_version(self):
        operator = get_user_model().objects.create_user("manutentore-demo", is_staff=True)
        identity = {
            "external_id": "PROVA-01",
            "first_name": "Giovanna",
            "last_name": "Prova",
            "section": "Demo",
            "email": "prova@example.invalid",
        }
        election = Election.objects.create(
            title="Consultazione sintetica", created_by=operator, closes_at=timezone.now(),
        )
        voter = ElectionVoter.objects.create(
            election=election, member=Member.objects.create(**identity), **identity,
        )
        previous_key = Fernet.generate_key().decode("ascii")
        next_key = Fernet.generate_key().decode("ascii")
        with self.settings(VOTING_LINK_KEYS=[previous_key]):
            credential = VotingCredential.objects.create(voter=voter, **credential_fields(), version=4)
            previous_url = personal_url(voter)
        with self.settings(VOTING_LINK_KEYS=[next_key, previous_key]):
            with self.assertRaises(CommandError):
                call_command("rotate_link_encryption", stdout=StringIO())
            call_command("rotate_link_encryption", apply=True, stdout=StringIO())
        reencrypted = VotingCredential.objects.get(pk=credential.pk)
        self.assertNotEqual(reencrypted.ciphertext, credential.ciphertext)
        self.assertEqual(reencrypted.version, credential.version)
        self.assertEqual(reencrypted.digest, credential.digest)
        with self.settings(VOTING_LINK_KEYS=[next_key]):
            call_command("rotate_link_encryption", stdout=StringIO())
            self.assertEqual(personal_url(voter), previous_url)
