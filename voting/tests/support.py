from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from members.models import Member
from voting import services
from voting.models import Choice, Election, Question


def staff():
    return get_user_model().objects.create(username="operatore", is_staff=True)


def election_with_motion(actor, title="Consultazione dimostrativa"):
    election = Election.objects.create(
        title=title,
        created_by=actor,
        closes_at=timezone.now() + timedelta(days=2),
    )
    question = Question.objects.create(
        election=election, text="Approvi la proposta?", kind=Question.Kind.MOTION
    )
    options = [
        Choice.objects.create(question=question, text=text)
        for text in ("Favorevole", "Contrario", "Astenuto")
    ]
    return election, question, options


def add_members(count=1):
    return [
        Member.objects.create(
            external_id=f"TEST-{index}",
            first_name=f"Socio {index}",
            last_name="Fittizio",
            email=f"socio{index}@example.invalid",
            section="Sezione Demo",
        )
        for index in range(count)
    ]


def open_election(election, members):
    with transaction.atomic():
        services.set_electorate(election, members)
        services.open_locked(services.lock_election(election.pk))
    election.refresh_from_db()
    return list(election.voters.all())


def redeem(client, voter):
    token = services.personal_url(voter).split("#", 1)[1]
    return client.post(reverse("voting:access"), {"token": token})


def review(client, revision, question=None, choice=None):
    data = {"revision": revision}
    if question:
        data[f"q{question.pk}"] = choice.pk if choice else ""
    return client.post(reverse("voting:ballot"), data)
