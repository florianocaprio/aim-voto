from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from mailings.models import MailKind
from voting import assembly, proxy_access, services
from voting.models import AuditEvent, Delegation, Election, NamedBallot, VoterPresence

from .support import (
    add_members,
    election_with_motion,
    open_election,
    redeem,
    review,
    staff,
)


@override_settings(REQUIRE_ADMIN_MFA=False)
class AssemblyTests(TestCase):
    def setUp(self):
        self.staff = staff()
        self.admin = Client()
        self.admin.force_login(self.staff)
        self.election, self.question, self.options = election_with_motion(self.staff)
        self.election.allow_delegations = True
        self.election.save(update_fields=["allow_delegations"])
        self.voter, self.proxy, self.other = open_election(
            self.election, add_members(3)
        )
        self.participation_url = reverse("voting:assembly")
        self.submit_url = reverse("voting:submit")
        redeem(self.client, self.voter)

    def request_delegation(self, client, delegate, election=None):
        client.post(
            self.participation_url,
            {"action": "delegate", "external_id": delegate.external_id},
        )
        election = election or self.election
        return Delegation.objects.filter(
            grantor__election=election, delegate=delegate
        ).first()

    def decide(self, election, delegation, action):
        path = reverse(
            "voting:staff_delegation", args=[election.pk, delegation.pk, action]
        )
        response = self.admin.post(path, follow=True)
        delegation.refresh_from_db()
        return response

    def test_confirmation_is_explicit_idempotent_and_separate_from_voting(self):
        self.client.get(reverse("voting:ballot"))
        self.client.get(self.participation_url)
        self.assertFalse(VoterPresence.objects.exists())
        for _ in range(2):
            self.assertContains(
                self.client.post(
                    self.participation_url, {"action": "presence"}, follow=True
                ),
                "Presenza confermata",
            )
        self.assertEqual(VoterPresence.objects.filter(voter=self.voter).count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(action="presenza_confermata").count(), 1
        )
        self.assertFalse(NamedBallot.objects.exists())
        url = reverse("voting:presence_csv", args=[self.election.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertContains(self.admin.get(url), self.voter.external_id)
        self.assertNotContains(self.admin.get(url), self.proxy.external_id)
        Election.objects.filter(pk=self.election.pk).update(
            status=Election.Status.CLOSED
        )
        redeem(self.client, self.other)
        self.client.post(self.participation_url, {"action": "presence"})
        self.assertEqual(VoterPresence.objects.count(), 1)

    def proxy_review(self, client, delegation, revision=0):
        path = reverse("voting:proxy_ballot", args=[delegation.pk])
        return client.post(
            path, {"revision": revision, f"q{self.question.pk}": self.options[0].pk}
        )

    def test_approved_delegate_can_vote_once_and_direct_confirmation_is_blocked(self):
        old_payload = review(self.client, 0, self.question, self.options[0]).context[
            "payload"
        ]
        delegation = self.request_delegation(self.client, self.proxy)
        self.assertIsNotNone(delegation)
        self.assertEqual(delegation.state, Delegation.State.PENDING)

        proxy = Client()
        outsider = Client()
        redeem(proxy, self.proxy)
        redeem(outsider, self.other)
        self.assertEqual(self.proxy_review(proxy, delegation).status_code, 404)
        self.decide(self.election, delegation, "approve")
        self.assertEqual(delegation.state, Delegation.State.APPROVED)
        self.assertEqual(self.proxy_review(outsider, delegation).status_code, 404)
        self.assertEqual(
            self.client.post(self.submit_url, {"payload": old_payload}).status_code, 409
        )

        payload = self.proxy_review(proxy, delegation).context["payload"]
        self.assertEqual(
            outsider.post(self.submit_url, {"payload": payload}).status_code, 400
        )
        self.assertContains(
            proxy.post(self.submit_url, {"payload": payload}), "Scheda corrente"
        )
        self.assertContains(
            proxy.post(self.submit_url, {"payload": payload}), "Scheda corrente"
        )
        self.assertEqual(NamedBallot.objects.get(voter=self.voter).cast_by, self.proxy)
        own = review(proxy, 0, self.question, self.options[2]).context["payload"]
        self.assertContains(
            proxy.post(self.submit_url, {"payload": own}), "Scheda corrente"
        )
        self.assertEqual(NamedBallot.objects.count(), 2)
        presence = VoterPresence.objects.get()
        self.assertEqual(presence.voter, self.proxy)
        self.assertEqual(presence.source, VoterPresence.Source.BALLOT)

        denied = self.decide(self.election, delegation, "revoke")
        self.assertContains(denied, "ha già registrato una scheda")
        self.assertEqual(delegation.state, Delegation.State.APPROVED)

    def test_required_presence_unlocks_ballot_and_is_registered_once(self):
        self.election.require_presence = True
        self.election.save(update_fields=["require_presence"])
        ballot = reverse("voting:ballot")
        before = self.client.get(ballot)
        self.assertContains(before, "Conferma presenza e vai al voto")
        self.assertNotContains(before, self.question.text)
        self.assertEqual(
            review(self.client, 0, self.question, self.options[0]).status_code, 409
        )
        self.assertFalse(VoterPresence.objects.exists())
        self.assertRedirects(
            self.client.post(ballot, {"action": "confirm_presence"}), ballot
        )
        self.assertContains(self.client.get(ballot), self.question.text)
        payload = review(self.client, 0, self.question, self.options[0]).context[
            "payload"
        ]
        self.assertContains(
            self.client.post(self.submit_url, {"payload": payload}),
            "Schede acquisite per 1 di 3 aventi diritto",
        )
        presence = VoterPresence.objects.get()
        self.assertEqual(presence.voter, self.voter)
        self.assertEqual(presence.source, VoterPresence.Source.EXPLICIT)

    def test_search_confirm_and_cast_direct_delegation(self):
        self.election.require_presence = True
        self.election.save(update_fields=["require_presence"])
        self.proxy.first_name = "Àda"
        self.proxy.last_name = "Bianchi"
        self.proxy.save(update_fields=["first_name", "last_name"])
        other_election, *_ = election_with_motion(
            self.staff, title="Consultazione separata"
        )
        outsider = open_election(other_election, [self.other.member])[0]
        find = reverse("voting:find_delegate")
        self.assertEqual(Client().get(find, {"format": "json"}).status_code, 403)
        self.assertEqual(
            self.client.get(
                reverse("voting:confirm_direct_delegation", args=[outsider.pk])
            ).status_code,
            404,
        )
        (candidate,) = self.client.get(
            find, {"q": "bian ada", "format": "json"}
        ).json()["candidates"]
        self.assertEqual(candidate["first_name"], "Àda")
        self.assertEqual(
            set(candidate), {"first_name", "last_name", "external_id", "url"}
        )
        self.assertContains(
            self.client.get(find, {"q": self.proxy.external_id[-3:]}), "Àda Bianchi"
        )

        confirm = candidate["url"]
        self.assertContains(self.client.get(confirm), self.proxy.external_id)
        self.assertEqual(self.client.post(confirm).status_code, 409)
        self.assertFalse(self.voter.delegations_given.exists())
        for _ in range(2):
            self.assertEqual(
                self.client.post(confirm, {"confirm": "yes"}).status_code, 302
            )
        delegation = self.voter.delegations_given.get(state=Delegation.State.APPROVED)
        self.assertEqual(delegation.delegate, self.proxy)
        self.assertEqual(delegation.notice.kind, MailKind.PROXY)
        self.assertEqual(delegation.notice.voter, self.proxy)
        self.assertContains(
            self.client.get(reverse("voting:ballot")), "spetta al delegato autorizzato"
        )
        self.assertEqual(
            review(self.client, 0, self.question, self.options[0]).status_code, 409
        )

        scoped = Client()
        token = urlsplit(proxy_access.link(delegation, self.proxy.credential)).fragment
        path = reverse("voting:proxy_ballot", args=[delegation.pk])
        self.assertEqual(
            scoped.post(reverse("voting:access"), {"token": token}).json()[
                "ballot_url"
            ],
            path,
        )
        self.assertEqual(scoped.get(find, {"format": "json"}).status_code, 403)
        self.assertContains(scoped.get(path), "Conferma presenza e vai al voto")
        self.assertEqual(self.proxy_review(scoped, delegation).status_code, 409)
        self.assertRedirects(scoped.post(path, {"action": "confirm_presence"}), path)
        payload = self.proxy_review(scoped, delegation).context["payload"]
        self.assertContains(
            scoped.post(self.submit_url, {"payload": payload}),
            "Schede acquisite per 1 di 3 aventi diritto",
        )
        self.assertEqual(VoterPresence.objects.get().voter, self.proxy)
        self.assertEqual(NamedBallot.objects.get().voter, self.voter)

    def test_revocation_before_cast_rejects_prepared_proxy_ballot(self):
        delegation = self.request_delegation(self.client, self.proxy)
        self.decide(self.election, delegation, "approve")
        proxy = Client()
        redeem(proxy, self.proxy)
        payload = self.proxy_review(proxy, delegation).context["payload"]
        revoke = reverse("voting:voter_revoke", args=[delegation.pk])
        self.assertEqual(proxy.post(revoke).status_code, 404)
        self.assertEqual(self.client.post(revoke).status_code, 302)
        self.assertEqual(
            proxy.post(self.submit_url, {"payload": payload}).status_code, 409
        )
        direct = review(self.client, 0, self.question, self.options[1]).context[
            "payload"
        ]
        self.assertContains(
            self.client.post(self.submit_url, {"payload": direct}), "Scheda corrente"
        )

    def test_scope_of_approval_blocks_request_if_grantor_already_voted(self):
        delegation = self.request_delegation(self.client, self.proxy)
        own = review(self.client, 0, self.question, self.options[1]).context["payload"]
        self.client.post(self.submit_url, {"payload": own})
        error = self.decide(self.election, delegation, "approve")
        self.assertContains(error, "ha già una scheda registrata")
        self.assertEqual(delegation.state, Delegation.State.PENDING)

    def test_three_at_500_and_five_at_501_frozen_voters(self):
        from members.models import Member

        extra = Member.objects.bulk_create(
            [
                Member(
                    external_id=f"SOGLIA-{index:03}",
                    first_name=f"Socio {index:03}",
                    last_name="Demo",
                    email=f"soglia{index}@example.invalid",
                    section="Demo",
                )
                for index in range(501)
            ]
        )
        for count, max_allowed in ((500, 3), (501, 5)):
            with self.subTest(eligible=count):
                election, *_ = election_with_motion(
                    self.staff, title=f"Demo soglia {count}"
                )
                election.allow_delegations = True
                election.save(update_fields=["allow_delegations"])
                holder, *grantors = open_election(election, extra[:count])
                for index, grantor in enumerate(grantors[: max_allowed + 1]):
                    client = Client()
                    self.assertEqual(redeem(client, grantor).status_code, 200)
                    delegation = self.request_delegation(client, holder, election)
                    self.assertIsNotNone(delegation)
                    response = self.decide(election, delegation, "approve")
                    if index < max_allowed:
                        self.assertEqual(delegation.state, Delegation.State.APPROVED)
                    else:
                        self.assertContains(
                            response, f"limite di {max_allowed} deleghe"
                        )
                        self.assertEqual(delegation.state, Delegation.State.PENDING)
                self.assertEqual(
                    holder.delegations_received.filter(
                        state=Delegation.State.APPROVED
                    ).count(),
                    max_allowed,
                )
                last_request = grantors[max_allowed].delegations_given.get()
                self.decide(election, last_request, "revoke")
                election.require_presence = True
                election.save(update_fields=["require_presence"])
                confirm = reverse("voting:confirm_direct_delegation", args=[holder.pk])
                self.assertContains(
                    client.post(confirm, {"confirm": "yes"}),
                    f"limite di {max_allowed} deleghe",
                    status_code=409,
                )
                previous = grantors[0].delegations_given.get()
                self.decide(election, previous, "revoke")
                self.assertEqual(
                    client.post(confirm, {"confirm": "yes"}).status_code, 302
                )
                self.assertEqual(
                    grantors[max_allowed]
                    .delegations_given.get(state=Delegation.State.APPROVED)
                    .notice.kind,
                    MailKind.PROXY,
                )


class ConcurrentDelegationTests(TransactionTestCase):
    def test_only_one_concurrent_approval_can_take_last_slot(self):
        actor = staff()
        election, *_ = election_with_motion(actor)
        election.allow_delegations = True
        election.save(update_fields=["allow_delegations"])
        delegate, *grantors = open_election(election, add_members(5))
        with transaction.atomic():
            locked = services.lock_election(election.pk)
            requests = [
                assembly.request_delegation(locked, grantor, delegate.external_id)
                for grantor in grantors
            ]
            for item in requests[:2]:
                assembly.decide_delegation(locked, item, "approve", actor=actor)

        start = Barrier(2, timeout=5)

        def approve(pk):
            try:
                start.wait()
                with transaction.atomic():
                    locked = services.lock_election(election.pk)
                    item = Delegation.objects.get(pk=pk)
                    assembly.decide_delegation(locked, item, "approve", actor=actor)
            except ValidationError as exc:
                return exc.messages[0]
            finally:
                connection.close()
            return "approved"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(approve, (item.pk for item in requests[2:])))
        self.assertEqual(outcomes.count("approved"), 1)
        self.assertIn("Il delegato ha raggiunto il limite di 3 deleghe.", outcomes)
