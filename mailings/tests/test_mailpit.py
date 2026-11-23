import csv
import json
import os
import re
from datetime import timedelta
from io import StringIO
from unittest import skipUnless
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mailings.models import DispatchSchedule, MailKind
from mailings.worker import process_next
from members.importing import IMPORT_FIELDS
from voting.models import Election, NamedBallot, Question

MAILPIT_API_URL = os.environ.get("MAILPIT_API_URL", "").rstrip("/")


@skipUnless(
    MAILPIT_API_URL,
    "Configurare MAILPIT_API_URL per lo scenario con SMTP reale in Mailpit.",
)
@override_settings(REQUIRE_ADMIN_MFA=False)
class MailpitFlowTests(TransactionTestCase):
    def setUp(self):
        assert urlsplit(MAILPIT_API_URL).hostname in {
            "mailpit",
            "localhost",
            "127.0.0.1",
            "::1",
        }
        admin = get_user_model().objects.create(
            username="operatore-end-to-end", is_staff=True, is_superuser=True
        )
        self.client.force_login(admin)
        run_id = uuid4().hex[:12]
        self.invite_subject = f"Inviti collaudo {run_id}"
        self.reminder_subject = f"Sollecito collaudo {run_id}"

    def import_electorate(self, election):
        source = self.client.get(reverse("members:demo-csv", args=[election.pk]))
        self.assertEqual(source.status_code, 200)
        upload = self.client.post(
            reverse("members:import", args=[election.pk]),
            {
                "file": SimpleUploadedFile(
                    "soci-demo.csv", source.content, content_type="text/csv"
                ),
                "encoding": "utf-8-sig",
                "separator": ";",
                "has_header": "on",
            },
        )
        self.assertEqual(upload.status_code, 302)
        mapping = self.client.post(
            upload.url, {name: str(index) for index, name in enumerate(IMPORT_FIELDS)}
        )
        self.assertEqual(mapping.status_code, 302)
        preview = self.client.get(mapping.url)
        self.assertEqual(preview.status_code, 200)
        fingerprint = preview.context["form"].initial["fingerprint"]
        self.assertEqual(
            self.client.post(
                mapping.url, {"fingerprint": fingerprint, "confirmed": "on"}
            ).status_code,
            302,
        )
        self.assertEqual(election.voters.count(), 3)

    def open_election(self):
        tomorrow = timezone.localtime().replace(hour=12, minute=0) + timedelta(days=1)
        created = self.client.post(
            reverse("voting:create"),
            {
                "title": "Consultazione dimostrativa",
                "mode": Election.Mode.NAMED,
                "closes_at": tomorrow.strftime("%Y-%m-%dT%H:%M"),
                "allow_replacement": "on",
            },
        )
        self.assertEqual(created.status_code, 302)
        election = Election.objects.get()
        self.import_electorate(election)
        self.assertEqual(
            self.client.post(
                reverse("voting:question_add", args=(election.pk,)),
                {
                    "text": "Mozione di prova",
                    "kind": Question.Kind.MOTION,
                    "max_choices": 1,
                },
            ).status_code,
            302,
        )
        self.transition(election, "apri")
        return election

    def transition(self, election, action):
        self.assertEqual(
            self.client.post(
                reverse("voting:transition", args=(election.pk, action)),
                {"confirm": "yes"},
            ).status_code,
            302,
        )

    def send_campaign(self, election, route, subject, expected_count):
        prepared = self.client.post(
            reverse(f"mailings:{route}", args=(election.pk,)),
            {"subject": subject, "body": "Messaggio sintetico di collaudo."},
        )
        self.assertEqual(prepared.status_code, 302)
        preview = self.client.get(prepared.url)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.context["counts"]["preview"], expected_count)
        campaign = preview.context["campaign"]
        self.assertEqual(
            self.client.post(
                reverse("mailings:confirm", args=(election.pk, campaign.pk))
            ).status_code,
            302,
        )
        for _ in range(expected_count):
            DispatchSchedule.objects.update(next_at=timezone.now())
            self.assertTrue(process_next())
        DispatchSchedule.objects.update(next_at=timezone.now())
        self.assertFalse(process_next())
        return campaign

    def mailpit_messages(self, subject):
        with urlopen(
            f"{MAILPIT_API_URL}/api/v1/messages?limit=100", timeout=5
        ) as response:
            messages = json.load(response)["messages"]
        return [message for message in messages if message["Subject"] == subject]

    def link_from_message(self, message):
        with urlopen(
            f"{MAILPIT_API_URL}/view/{message['ID']}.txt", timeout=5
        ) as response:
            body = response.read().decode("utf-8")
        matches = re.findall(
            r"^Il tuo link personale: (\S+)[ \t\r]*$", body, re.MULTILINE
        )
        self.assertEqual(
            len(matches), 1, "Il testo deve contenere un solo link personale."
        )
        return matches[0]

    def access_as_voter(self, link):
        client = Client()
        response = client.post(
            reverse("voting:access"), {"token": urlsplit(link).fragment}
        )
        self.assertEqual(response.status_code, 200)
        return client

    def vote(self, client, revision, question, choice):
        preview = client.post(
            reverse("voting:ballot"),
            {"revision": revision, f"q{question.pk}": choice.pk},
        )
        self.assertEqual(preview.status_code, 200)
        submitted = client.post(
            reverse("voting:submit"), {"payload": preview.context["payload"]}
        )
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.context["receipt"].revision, revision + 1)

    def test_three_voters_replacement_and_one_mass_reminder(self):
        election = self.open_election()
        voters = list(election.voters.order_by("external_id"))
        question = election.questions.get()
        for_choice = question.choices.get(text="Favorevole")
        against_choice = question.choices.get(text="Contrario")
        self.send_campaign(election, "invite", self.invite_subject, 3)
        invites = self.mailpit_messages(self.invite_subject)
        self.assertEqual(len(invites), 3)
        links = {}
        for invite in invites:
            self.assertEqual(len(invite["To"]), 1)
            self.assertFalse(invite["Cc"])
            self.assertFalse(invite["Bcc"])
            links[invite["To"][0]["Address"]] = self.link_from_message(invite)
        self.assertEqual(set(links), {voter.email for voter in voters})

        first = self.access_as_voter(links[voters[0].email])
        second = self.access_as_voter(links[voters[1].email])
        self.assertFalse(NamedBallot.objects.exists())
        self.vote(first, 0, question, for_choice)
        self.vote(second, 0, question, for_choice)
        self.vote(first, 1, question, against_choice)

        self.send_campaign(election, "remind", self.reminder_subject, 1)
        reminders = self.mailpit_messages(self.reminder_subject)
        self.assertEqual(len(reminders), 1)
        reminder = reminders[0]
        self.assertEqual(
            [recipient["Address"] for recipient in reminder["To"]], [voters[2].email]
        )
        self.assertFalse(reminder["Cc"])
        self.assertFalse(reminder["Bcc"])
        self.assertTrue(
            self.link_from_message(reminder) == links[voters[2].email],
            "Il sollecito deve riutilizzare il link per l’invito del terzo socio.",
        )
        copy = self.client.post(
            reverse("voting:copy_link", args=(election.pk, voters[2].pk))
        )
        self.assertEqual(copy.status_code, 200)
        self.assertTrue(copy.json()["url"] == links[voters[2].email])

        self.transition(election, "chiudi")
        self.assertFalse(election.campaigns.filter(kind=MailKind.RESULTS).exists())
        self.transition(election, "scrutina")
        campaign = election.campaigns.get(kind=MailKind.RESULTS)
        self.assertEqual(campaign.deliveries.count(), 3)
        self.assertContains(
            self.client.post(
                reverse("voting:transition", args=(election.pk, "scrutina")),
                {"confirm": "yes"},
            ),
            "Operazione non disponibile nello stato attuale",
        )
        sent_before = {mail["ID"] for mail in self.mailpit_messages(campaign.subject)}
        for _ in voters:
            DispatchSchedule.objects.update(next_at=timezone.now())
            self.assertTrue(process_next())
        emails = [
            mail
            for mail in self.mailpit_messages(campaign.subject)
            if mail["ID"] not in sent_before
        ]
        self.assertEqual(len(emails), 3)
        self.assertEqual({mail["To"][0]["Address"] for mail in emails}, set(links))
        for email in emails:
            self.assertFalse(email["Cc"])
            self.assertFalse(email["Bcc"])
            with urlopen(
                f"{MAILPIT_API_URL}/view/{email['ID']}.txt", timeout=5
            ) as response:
                body = response.read().decode("utf-8")
            for line in (
                "Aventi diritto: 3",
                "Votanti: 2",
                "Favorevole: 1",
                "Contrario: 1",
                "Astenuto: 0",
            ):
                self.assertIn(line, body)
            self.assertNotIn("/accedi/", body)
        results = self.client.get(reverse("voting:results", args=(election.pk,)))
        self.assertEqual(results.status_code, 200)
        self.assertEqual(results.context["total"], 2)
        self.assertEqual(
            dict(results.context["rows"][0]["choices"]),
            {"Favorevole": 1, "Contrario": 1, "Astenuto": 0},
        )

        participation = self.client.get(
            reverse("voting:export", args=(election.pk, "partecipazione-csv"))
        )
        self.assertEqual(participation.status_code, 200)
        rows = csv.DictReader(
            StringIO(participation.content.decode("utf-8-sig")), delimiter=";"
        )
        voted = {row["ID socio"]: row["Ha votato"] for row in rows}
        self.assertEqual(
            voted,
            {
                voters[0].external_id: "Sì",
                voters[1].external_id: "Sì",
                voters[2].external_id: "No",
            },
        )
