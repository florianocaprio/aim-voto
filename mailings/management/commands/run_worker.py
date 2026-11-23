from time import sleep

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from mailings.transport import check_local_transport
from mailings.worker import process_next, recover_interrupted
from voting.services import advance_schedules


class Command(BaseCommand):
    help = "Aggiorna le consultazioni programmate ed elabora la coda SMTP di sviluppo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Esegue un singolo passaggio della coda.",
        )

    def handle(self, *args, **options):
        check_local_transport()
        while True:
            close_old_connections()
            advance_schedules()
            recover_interrupted()
            processed = process_next()
            if options["once"]:
                return
            sleep(0.1 if processed else 1)
