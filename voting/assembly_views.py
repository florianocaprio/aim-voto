from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import assembly, reports, services
from .access import administrator
from .models import Delegation, Election, VoterPresence
from .views import SESSION_KEY, voter_context


def lock_voter(request):
    ids = request.session.get(SESSION_KEY)
    if not ids:
        raise PermissionDenied
    election = get_object_or_404(Election.objects.select_for_update(), pk=ids[1])
    voter = voter_context(request)
    if voter is None:
        raise PermissionDenied
    return election, voter


@require_http_methods(["GET", "POST"])
@transaction.atomic
def voter_assembly(request):
    if request.method == "POST":
        election, voter = lock_voter(request)
        action = request.POST.get("action")
        try:
            now = services.database_now()
            if not services.is_open(election, now):
                raise ValidationError("La consultazione è chiusa.")
            if action == "presence":
                _, created = VoterPresence.objects.get_or_create(
                    voter=voter, defaults={"confirmed_at": now}
                )
                if created:
                    services.audit(
                        "presenza_confermata", election=election, voter=voter
                    )
                messages.success(request, "Presenza confermata.")
            elif action == "delegate":
                external_id = request.POST.get("external_id", "").strip()
                if not external_id or len(external_id) > 64:
                    raise ValidationError("Inserisci l’ID socio del delegato.")
                if not services.admit_request(
                    "richiesta-delega", voter.pk, limit=8, seconds=60
                ):
                    raise ValidationError("Troppi tentativi. Riprova fra un minuto.")
                assembly.request_delegation(election, voter, external_id)
                messages.success(
                    request, "Delega presentata allo staff per la verifica."
                )
            else:
                raise Http404
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        return redirect("voting:assembly")
    voter = voter_context(request)
    if voter is None:
        return redirect("voting:ballot")
    election = voter.election
    active = (
        voter.delegations_given.filter(state__in=assembly.ACTIVE)
        .select_related("delegate")
        .first()
    )
    return render(
        request,
        "voting/assembly.html",
        {
            "voter": voter,
            "election": election,
            "is_open": services.is_open(election),
            "presence": VoterPresence.objects.filter(voter=voter).first(),
            "active": active,
            "last_request": voter.delegations_given.select_related("delegate").first(),
            "received": voter.delegations_received.filter(
                state=Delegation.State.APPROVED
            ).select_related("grantor"),
        },
    )


@require_POST
@transaction.atomic
def voter_revoke(request, delegation_pk):
    election, voter = lock_voter(request)
    delegation = get_object_or_404(voter.delegations_given, pk=delegation_pk)
    try:
        assembly.decide_delegation(election, delegation, "revoke")
        messages.success(request, "Delega revocata.")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("voting:assembly")


def presence_records(election):
    return (
        VoterPresence.objects.filter(voter__election=election)
        .select_related("voter")
        .order_by(
            "confirmed_at",
            "pk",
        )
    )


@administrator
@require_GET
def manage_assembly(request, pk):
    election = get_object_or_404(Election, pk=pk, opened_at__isnull=False)
    delegations = Delegation.objects.filter(
        grantor__election=election, delegate__election=election
    ).select_related("grantor", "delegate")
    return render(
        request,
        "voting/assembly_admin.html",
        {
            "election": election,
            "is_open": services.is_open(election),
            "presences": Paginator(presence_records(election), 40).get_page(
                request.GET.get("page")
            ),
            "delegations": delegations,
            "limit": assembly.delegation_limit(election.voters.count()),
        },
    )


@administrator
@require_GET
def presence_csv(request, pk):
    election = get_object_or_404(Election, pk=pk, opened_at__isnull=False)
    rows = [
        ["ID socio", "Nome", "Cognome", "Sezione", "Presenza confermata (Europe/Rome)"]
    ]
    for item in presence_records(election).iterator():
        voter = item.voter
        rows.append(
            [
                voter.external_id,
                voter.first_name,
                voter.last_name,
                voter.section,
                timezone.localtime(item.confirmed_at).isoformat(timespec="seconds"),
            ]
        )
    return reports.csv_response("presenze.csv", rows)


@administrator
@require_POST
@transaction.atomic
def staff_decision(request, pk, delegation_pk, action):
    election = get_object_or_404(Election.objects.select_for_update(), pk=pk)
    delegation = get_object_or_404(
        Delegation,
        pk=delegation_pk,
        grantor__election=election,
        delegate__election=election,
    )
    try:
        assembly.decide_delegation(election, delegation, action, actor=request.user)
        messages.success(
            request, f"Delega aggiornata. Stato: {delegation.get_state_display()}."
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("voting:manage_assembly", pk=pk)
