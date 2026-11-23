from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from members.demo import DEMO_MEMBERS
from voting.models import MAX_ELECTORATE_SIZE, Election
from voting.services import lock_election


class Command(BaseCommand):
    help = "Aggiunge tre elettori dimostrativi a una consultazione in bozza."

    def add_arguments(self, parser):
        parser.add_argument("election_id", type=int)

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            election = lock_election(options["election_id"])
        except Election.DoesNotExist as exc:
            raise CommandError("Consultazione non trovata.") from exc
        if election.status != Election.Status.DRAFT:
            raise CommandError("Gli elettori dimostrativi si aggiungono a una bozza.")
        existing = set(election.voters.values_list("external_id", flat=True))
        new = [row for row in DEMO_MEMBERS if row["external_id"] not in existing]
        if len(existing) + len(new) > MAX_ELECTORATE_SIZE:
            raise CommandError(
                f"La platea può contenere al massimo {MAX_ELECTORATE_SIZE} elettori."
            )
        for row in new:
            election.voters.create(**row)
        self.stdout.write(
            self.style.SUCCESS(f"Elettori dimostrativi creati: {len(new)}.")
        )
