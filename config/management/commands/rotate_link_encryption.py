from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from voting.models import VotingCredential
from voting.services import digest, link_cipher


class Command(BaseCommand):
    help = "Verifica o ricifra le credenziali con la prima chiave di VOTING_LINK_KEYS."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Applica la nuova cifratura.")

    def handle(self, *args, **options):
        key = Fernet(settings.VOTING_LINK_KEYS[0])
        cipher = link_cipher()
        changed = invalid = 0
        for credential_id in VotingCredential.objects.values_list("pk", flat=True).iterator():
            with transaction.atomic():
                credential = VotingCredential.objects.select_for_update().filter(pk=credential_id).first()
                if credential is None:
                    continue
                ciphertext = bytes(credential.ciphertext)
                try:
                    plaintext = cipher.decrypt(ciphertext)
                    if digest(plaintext.decode("ascii")) != credential.digest:
                        raise InvalidToken
                except (InvalidToken, UnicodeDecodeError):
                    invalid += 1
                    continue
                try:
                    key.decrypt(ciphertext)
                except InvalidToken:
                    changed += 1
                    if options["apply"]:
                        credential.ciphertext = cipher.rotate(ciphertext)
                        credential.save(update_fields=["ciphertext"])
        if options["apply"]:
            self.stdout.write(f"Credenziali ricifrate: {changed}.")
        else:
            self.stdout.write(f"Credenziali da ricifrare: {changed}.")
        if invalid:
            raise CommandError(f"Credenziali non verificabili con le chiavi disponibili: {invalid}.")
        if changed and not options["apply"]:
            raise CommandError("La prima chiave non può ancora sostituire da sola le precedenti.")
