import base64
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice


class Command(BaseCommand):
    help = "Configura o sostituisce il TOTP di un account staff da un terminale privato."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--replace", action="store_true")

    def handle(self, *args, **options):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise CommandError("Apri un terminale privato senza reindirizzare l'output.")
        with transaction.atomic():
            try:
                user = get_user_model().objects.select_for_update().get(
                    username=options["username"], is_staff=True, is_active=True
                )
            except get_user_model().DoesNotExist:
                raise CommandError("Account staff attivo non trovato.") from None
            devices = TOTPDevice.objects.filter(user=user)
            if devices.exists():
                if not options["replace"]:
                    raise CommandError("TOTP già configurato. Usa --replace dopo verifica dell'identità.")
                if input("Scrivi SOSTITUISCI per revocare il dispositivo precedente: ") != "SOSTITUISCI":
                    raise CommandError("Sostituzione annullata.")
                devices.delete()
            device = TOTPDevice.objects.create(user=user, name="AIM Voto", confirmed=True)
        enrollment_secret = base64.b32encode(device.bin_key).decode("ascii").rstrip("=")
        self.stdout.write("Inserisci la chiave in un'app TOTP (6 cifre, SHA-1, 30 secondi).")
        self.stdout.write("Conserva il secondo fattore esclusivamente nell'app di autenticazione.")
        self.stdout.write(enrollment_secret)
