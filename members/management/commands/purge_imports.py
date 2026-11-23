from django.core.management.base import BaseCommand
from django.utils import timezone

from members.models import ImportBatch


class Command(BaseCommand):
    help = "Elimina i caricamenti temporanei scaduti."

    def handle(self, *args, **options):
        deleted, _ = ImportBatch.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(f"Caricamenti scaduti eliminati: {deleted}.")
