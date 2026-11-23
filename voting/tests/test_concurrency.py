from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from time import sleep
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TransactionTestCase

from voting import services
from voting.models import NamedBallot, SubmissionReceipt

from .support import add_members, election_with_motion, open_election, staff


class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.election, self.question, self.options = election_with_motion(staff())
        (self.voter,) = open_election(self.election, add_members())

    def submit(self, request_id, option, revision=0, ready=None):
        try:
            if ready:
                ready()
            return services.record_ballot(
                self.voter.pk,
                self.election.pk,
                1,
                request_id,
                revision,
                {str(self.question.pk): [option.pk]},
            )[0].revision
        except ValidationError as exc:
            return exc.messages[0]
        finally:
            connection.close()

    def test_two_confirmations_of_same_revision_require_one_fresh_confirmation(self):
        barrier = Barrier(2, timeout=5)
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(
                self.submit, uuid4(), self.options[0], ready=barrier.wait
            )
            second = workers.submit(
                self.submit, uuid4(), self.options[1], ready=barrier.wait
            )
            outcomes = [first.result(timeout=8), second.result(timeout=8)]
        self.assertEqual(outcomes.count(1), 1)
        self.assertIn(
            "La scheda è cambiata",
            next(outcome for outcome in outcomes if outcome != 1),
        )
        self.assertEqual(NamedBallot.objects.count(), 1)
        self.assertEqual(SubmissionReceipt.objects.count(), 1)

    def test_concurrent_duplicate_returns_same_receipt(self):
        barrier = Barrier(2, timeout=5)
        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = [
                workers.submit(self.submit, key, self.options[0], ready=barrier.wait)
                for _ in range(2)
            ]
            self.assertEqual(
                [first.result(timeout=8), second.result(timeout=8)], [1, 1]
            )
        self.assertEqual(SubmissionReceipt.objects.count(), 1)

    def test_waiting_confirmation_checks_deadline_after_taking_lock(self):
        self.assertEqual(self.submit(uuid4(), self.options[0]), 1)
        started = Event()
        with ThreadPoolExecutor(max_workers=1) as workers:
            with transaction.atomic():
                locked = services.lock_election(self.election.pk)
                locked.closes_at = services.database_now() + timedelta(milliseconds=350)
                locked.save(update_fields=["closes_at"])
                later = workers.submit(
                    self.submit, uuid4(), self.options[1], 1, started.set
                )
                self.assertTrue(started.wait(timeout=5))
                while services.database_now() < locked.closes_at:
                    sleep(0.01)
            self.assertIn("Votazione chiusa", later.result(timeout=8))
        self.assertEqual(
            NamedBallot.objects.get().answers,
            {str(self.question.pk): [self.options[0].pk]},
        )

    def test_manual_close_and_confirmation_share_lock(self):
        started = Event()
        with ThreadPoolExecutor(max_workers=1) as workers:
            with transaction.atomic():
                election = services.lock_election(self.election.pk)
                later = workers.submit(
                    self.submit, uuid4(), self.options[0], ready=started.set
                )
                self.assertTrue(started.wait(timeout=5))
                services.close_locked(election)
            self.assertIn("Votazione chiusa", later.result(timeout=8))
        self.assertFalse(NamedBallot.objects.exists())
