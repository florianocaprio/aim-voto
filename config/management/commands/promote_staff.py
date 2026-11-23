from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from voting.services import audit


class Command(BaseCommand):
    help = "Promuove un account staff attivo a superadmin, mantenendo password e secondo fattore."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--rename", metavar="NUOVO_NOME")

    def handle(self, *args, **options):
        model = get_user_model()
        username = options["username"]
        target = options["rename"] or username
        try:
            with transaction.atomic():
                account = (
                    model.objects.select_for_update().filter(username=username).first()
                )
                if account is None and target != username:
                    if model.objects.filter(
                        username=target,
                        is_staff=True,
                        is_superuser=True,
                        is_active=True,
                    ).exists():
                        self.stdout.write("Account superadmin già configurato.")
                        return
                if account is None or not account.is_active or not account.is_staff:
                    raise CommandError("Account staff attivo non trovato.")
                if account.is_superuser and account.username == target:
                    self.stdout.write("Account superadmin già configurato.")
                    return
                account.username = target
                account.is_superuser = True
                account.full_clean()
                account.save(update_fields=["username", "is_superuser"])
                audit("account_promosso_superadmin", actor=account)
        except (ValidationError, IntegrityError) as error:
            raise CommandError(
                "Verifica che il nuovo nome utente sia valido e disponibile."
            ) from error
        self.stdout.write(f"Account superadmin configurato come {target}.")
