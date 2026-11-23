from io import BytesIO

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import assembly, reports, services
from .access import election_administrator
from .forms import ReceivedDelegationForm
from .models import Delegation, DelegationAttachment, Election, VoterPresence
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


def require_direct_delegation(election):
    if not (election.require_presence and services.delegations_open(election)):
        raise Http404


@require_GET
def find_delegate(request):
    return delegation_search(request, received=False)


@require_http_methods(["GET", "POST"])
def find_grantor(request):
    return delegation_search(request, received=True)


def delegation_search(request, *, received):
    voter = voter_context(request)
    as_json = request.GET.get("format") == "json"
    if voter is None:
        if request.method == "POST":
            raise PermissionDenied
        return (
            JsonResponse({"error": "Riapri il link personale."}, status=403)
            if as_json
            else redirect("voting:ballot")
        )
    election = voter.election
    if received:
        if not services.delegations_open(election):
            raise Http404
        if request.method == "POST":
            messages.error(
                request, "Cerca il delegante prima di allegare il documento."
            )
            return redirect("voting:received_delegation")
        selection_route = "voting:received_delegation_file"
    else:
        require_direct_delegation(election)
        selection_route = "voting:confirm_direct_delegation"
    if not received and (
        voter.delegations_given.filter(state__in=assembly.ACTIVE).exists()
        or election.participation_records().filter(voter=voter).exists()
    ):
        raise Http404
    query = request.GET.get("q", "").strip()[:100]
    candidates = []
    error = ""
    if query:
        if services.admit_request("ricerca-delegato", voter.pk, limit=60, seconds=60):
            candidates = assembly.search_delegates(voter, query)
            at_capacity = set()
            if candidates and not received:
                limit = assembly.delegation_limit(election.voters.count())
                at_capacity = set(
                    assembly.reserved_slots(election)
                    .filter(
                        delegate_id__in=[candidate["pk"] for candidate in candidates]
                    )
                    .order_by()
                    .values("delegate_id")
                    .annotate(total=Count("pk"))
                    .filter(total__gte=limit)
                    .values_list("delegate_id", flat=True)
                )
            for candidate in candidates:
                pk = candidate.pop("pk")
                if pk in at_capacity:
                    candidate["unavailable"] = assembly.capacity_message(limit)
                else:
                    candidate["url"] = reverse(selection_route, args=[pk])
        else:
            error = "Troppi tentativi. Riprova fra un minuto."
    status = 429 if error else 200
    if as_json:
        return JsonResponse({"candidates": candidates, "error": error}, status=status)
    return render(
        request,
        "voting/find_delegate.html",
        {
            "election": election,
            "query": query,
            "candidates": candidates,
            "error": error,
            "received": received,
        },
        status=status,
    )


@require_http_methods(["GET", "POST"])
@transaction.atomic
def confirm_direct_delegation(request, delegate_pk):
    if request.method == "POST":
        election, voter = lock_voter(request)
    else:
        voter = voter_context(request)
        if voter is None:
            return redirect("voting:ballot")
        election = voter.election
    require_direct_delegation(election)
    delegate = get_object_or_404(election.voters.exclude(pk=voter.pk), pk=delegate_pk)
    error = ""
    try:
        if request.method == "POST":
            if request.POST.get("confirm") != "yes":
                raise ValidationError("Conferma la scelta per conferire la delega.")
            assembly.assign_direct_delegation(election, voter, delegate)
            messages.success(
                request,
                "Delega conferita. Il delegato riceverà il link dedicato all’apertura del voto."
                if services.is_delegation_phase(election)
                else "Delega conferita. Il link è stato accodato per l’invio al delegato.",
            )
            return redirect("voting:ballot")
        if (
            voter.delegations_given.filter(state__in=assembly.ACTIVE).exists()
            or election.participation_records().filter(voter=voter).exists()
        ):
            return redirect("voting:ballot")
        assembly.require_delegation_capacity(election, delegate.pk)
    except assembly.DelegateAtCapacity as exc:
        messages.error(request, exc.messages[0])
        return redirect("voting:find_delegate")
    except ValidationError as exc:
        error = exc.messages[0]
    return render(
        request,
        "voting/confirm_direct_delegation.html",
        {"election": election, "delegate": delegate, "error": error},
        status=409 if error else 200,
    )


@require_http_methods(["GET", "POST"])
@transaction.atomic
def voter_assembly(request):
    if request.method == "POST":
        election, voter = lock_voter(request)
        action = request.POST.get("action")
        try:
            now = services.database_now()
            if action == "presence":
                if not services.is_open(election, now):
                    raise ValidationError(
                        "La presenza si può confermare quando è aperta la votazione."
                    )
                services.register_presence(
                    election, voter.pk, now, VoterPresence.Source.EXPLICIT
                )
                messages.success(request, "Presenza confermata.")
            elif action == "delegate":
                if election.require_presence:
                    raise ValidationError(
                        "Usa la ricerca per conferire la delega diretta."
                    )
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
    incoming = (
        voter.delegations_received.filter(grantor__election=election)
        .select_related("grantor")
        .annotate(exercised=Exists(assembly.ballots_cast_by_delegate(election)))
    )
    return render(
        request,
        "voting/assembly.html",
        {
            "election": election,
            "is_open": services.is_open(election),
            "delegations_open": services.delegations_open(election),
            "has_voted": election.participation_records().filter(voter=voter).exists(),
            "has_exercised_delegation": incoming.filter(
                state=Delegation.State.APPROVED, exercised=True
            ).exists(),
            "active": active,
            "last_request": voter.delegations_given.select_related("delegate").first(),
            "received": incoming.filter(
                state=Delegation.State.APPROVED,
                attachment__isnull=True,
            ),
            "documented": incoming.filter(attachment__isnull=False),
        },
    )


@require_POST
@transaction.atomic
def voter_revoke(request, delegation_pk):
    election, voter = lock_voter(request)
    allowed = Delegation.objects.filter(
        Q(grantor=voter)
        | Q(delegate=voter, attachment__isnull=False, state=Delegation.State.PENDING),
        grantor__election=election,
        delegate__election=election,
    )
    delegation = get_object_or_404(allowed, pk=delegation_pk)
    try:
        assembly.decide_delegation(election, delegation, "revoke")
        messages.success(request, "Delega revocata.")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("voting:assembly")


@require_http_methods(["GET", "POST"])
@transaction.atomic
def submit_received_delegation(request, grantor_pk):
    if request.method == "POST":
        election, voter = lock_voter(request)
    else:
        voter = voter_context(request)
        if voter is None:
            return redirect("voting:ballot")
        election = voter.election
    if not services.delegations_open(election):
        raise Http404
    grantor = get_object_or_404(election.voters.exclude(pk=voter.pk), pk=grantor_pk)
    limit = assembly.delegation_limit(election.voters.count())
    reserved = assembly.reserved_slots(election, voter.pk).count()
    at_capacity = reserved >= limit
    form = ReceivedDelegationForm(
        request.POST if request.method == "POST" and not at_capacity else None,
        request.FILES if request.method == "POST" and not at_capacity else None,
    )
    if request.method == "POST" and not at_capacity and form.is_valid():
        if not services.admit_request(
            "delega-documentale", voter.pk, limit=8, seconds=60
        ):
            form.add_error(None, "Troppi tentativi. Riprova fra un minuto.")
        else:
            try:
                with transaction.atomic():
                    assembly.request_received_delegation(
                        election, voter, grantor, form.cleaned_data["file"]
                    )
            except ValidationError as exc:
                form.add_error(None, exc)
            except DatabaseError:
                form.add_error(
                    "file",
                    "Salvataggio del documento non riuscito. Seleziona di nuovo il file e riprova.",
                )
            else:
                messages.success(
                    request, "Richiesta con allegato presentata allo staff."
                )
                return redirect("voting:assembly")
    return render(
        request,
        "voting/received_delegation.html",
        {
            "election": election,
            "grantor": grantor,
            "form": form,
            "reserved": reserved,
            "limit": limit,
            "at_capacity": at_capacity,
        },
    )


@election_administrator
@require_GET
def manage_assembly(request, pk):
    election = get_object_or_404(
        Election,
        Q(opened_at__isnull=False) | Q(delegations_opened_at__isnull=False),
        pk=pk,
    )
    delegations = (
        Delegation.objects.filter(
            grantor__election=election, delegate__election=election
        )
        .select_related("grantor", "delegate", "notice")
        .annotate(
            has_attachment=Exists(
                DelegationAttachment.objects.filter(delegation_id=OuterRef("pk"))
            )
        )
    )
    return render(
        request,
        "voting/assembly_admin.html",
        {
            "election": election,
            "is_open": services.delegations_open(election),
            "presences": Paginator(reports.presence_records(election), 40).get_page(
                request.GET.get("page")
            ),
            "delegations": delegations,
            "limit": assembly.delegation_limit(election.voters.count()),
        },
    )


@election_administrator
@require_GET
def presence_csv(request, pk):
    election = get_object_or_404(Election, pk=pk, opened_at__isnull=False)
    return reports.presences_csv(election)


@election_administrator
@require_GET
def download_delegation_attachment(request, pk, delegation_pk):
    attachment = get_object_or_404(
        DelegationAttachment.objects.select_related("delegation__grantor"),
        delegation_id=delegation_pk,
        delegation__grantor__election_id=pk,
        delegation__delegate__election_id=pk,
    )
    services.audit(
        "allegato_delega_scaricato",
        actor=request.user,
        election=attachment.delegation.grantor.election,
        voter=attachment.delegation.grantor,
        details={"delega": delegation_pk},
    )
    return FileResponse(
        BytesIO(bytes(attachment.content)),
        content_type=attachment.mime_type,
        as_attachment=True,
        filename=attachment.filename,
    )


@election_administrator
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
