from django.core.management.base import BaseCommand
from django.db import transaction

from members.demo import DEMO_MEMBERS
from members.models import Member


class Command(BaseCommand):
    help = "Aggiunge tre soci dimostrativi con recapiti example.invalid."

    @transaction.atomic
    def handle(self, *args, **options):
        created_count = 0
        for row in DEMO_MEMBERS:
            _, created = Member.objects.get_or_create(
                external_id=row["external_id"], defaults=row
            )
            created_count += int(created)
        self.stdout.write(
            self.style.SUCCESS(f"Soci dimostrativi creati: {created_count}.")
        )
