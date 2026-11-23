import sys
from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError


class Command(BaseCommand):
    help = "Crea un account amministrativo con password inserita in un terminale privato."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise CommandError("Apri un terminale interattivo per inserire la password.")
        user = get_user_model()(username=options["username"], is_staff=True)
        password = getpass("Password amministrativa: ")
        if password != getpass("Ripeti la password: "):
            raise CommandError("Le due password non coincidono.")
        try:
            validate_password(password, user)
            user.set_password(password)
            user.full_clean()
            user.save(force_insert=True)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from None
        except IntegrityError:
            raise CommandError("Username già presente.") from None
        self.stdout.write("Account amministrativo creato.")
