import re
from io import BytesIO
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, Exists, OuterRef, Q, Subquery, Value, When
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.middleware.csrf import rotate_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from config.auth import require_superadmin, staff_required, superadmin_required
from mailings.campaigns import enqueue_results

from . import assembly, assembly_report, proxy_access, quorum, reports, services
from .access import election_administrator, has_global_access, managed_elections
from .forms import (
    AssemblyMinutesForm,
    AssemblyVerificationForm,
    ContactForm,
    DocumentForm,
    ElectionForm,
    QuestionForm,
    ReasonForm,
    VoterForm,
)
from .models import (
    MAX_ELECTORATE_SIZE,
    AssemblyVerification,
    Choice,
    Delegation,
    Election,
    ElectionDocument,
    ElectionVoter,
    Question,
    VoterPresence,
    VotingCredential,
    VotingQuorum,
)

# Require a link redemption with CSRF rotation before accepting votes from older tabs.
SESSION_KEY = "vote_access_v2"
SUBMISSION_SALT = "aim-voto.ballot-preview"


def home(request):
    return render(request, "voting/home.html")


@staff_required
@require_GET
def dashboard(request):
    return render(
        request,
        "voting/dashboard.html",
        {
            "elections": managed_elections(request.user),
            "can_create": has_global_access(request.user),
        },
    )


def get_election(pk, lock=False):
    queryset = Election.objects.select_for_update() if lock else Election
    return get_object_or_404(queryset, pk=pk)


def require_draft(election):
    if election.status != Election.Status.DRAFT:
        raise Http404


def form_page(request, title, form, election=None, *, submit_text="Salva", **context):
    return render(
        request,
        "voting/form.html",
        {
            "title": title,
            "form": form,
            "election": election,
            "submit_text": submit_text,
            **context,
        },
    )


@election_administrator
@require_http_methods(["GET", "POST"])
@transaction.atomic
def election_edit(request, pk=None):
    election = get_election(pk, request.method == "POST") if pk is not None else None
    if election:
        require_draft(election)
    form = ElectionForm(
        request.POST if request.method == "POST" else None, instance=election
    )
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        if election is None:
            saved.created_by = request.user
        saved.save()
        services.audit("bozza_salvata", actor=request.user, election=saved)
        return redirect("voting:detail", pk=saved.pk)
    return form_page(
        request,
        "Modifica consultazione" if election else "Nuova consultazione",
        form,
        election,
    )


def question_summary(question, election, electorate_count, assembly_rules):
    if assembly_rules:
        matter = assembly_rules["agenda"].get(str(question.pk))
        if matter:
            rule = matter["question_quorum"]
            minimum = (
                quorum.Rule(**rule).minimum(assembly_rules["eligible"])
                if rule
                else None
            )
            return {
                "label": matter["question_quorum_label"],
                "minimum": minimum,
                "matter": matter,
            }
    rule = quorum.question_constitution_rule(
        question.constitutive_quorum,
        question.constitutive_fraction,
        question.minimum_voters,
    )
    if rule is not None:
        return quorum.question_quorum_outcome(rule, electorate_count, 0)
    return reports.participation_quorum(
        question.voting_quorum or election.voting_quorum, electorate_count, 0
    ) or {"label": "Non configurato"}


@election_administrator
@require_GET
def detail(request, pk):
    election = get_election(pk)
    totals = reports.turnout(election)
    electorate_count, ballot_count = totals["eligible"], totals["voted"]
    from mailings.models import EmailDelivery, MailKind

    mail = EmailDelivery.objects.filter(
        voter__election=election,
        kind__in=[MailKind.INVITE, MailKind.RESEND, MailKind.EARLY, MailKind.VOTE_OPEN],
    )
    mail_states = mail.values_list("voter_id", "state").distinct()
    accepted = {voter_id for voter_id, state in mail_states if state == "accepted"}
    problems = {
        voter_id for voter_id, state in mail_states if state in {"failed", "uncertain"}
    }
    assembly_rules = election.assembly_snapshot
    assembly_error = ""
    if election.assembly_type and assembly_rules is None:
        try:
            assembly_rules = quorum.configuration(election, electorate_count)
        except ValidationError as error:
            assembly_error = error.messages[0]
    verification = (
        election.assembly_verifications.first() if election.assembly_snapshot else None
    )
    return render(
        request,
        "voting/detail.html",
        {
            "election": election,
            "questions": [
                (
                    question,
                    question_summary(
                        question, election, electorate_count, assembly_rules
                    ),
                )
                for question in services.ballot_questions(election)
            ],
            "assembly_rules": assembly_rules,
            "assembly_error": assembly_error,
            "verification": verification,
            "documents": election.documents.defer("content"),
            "electorate_count": electorate_count,
            "ballot_count": ballot_count,
            "delegation_phase": services.is_delegation_phase(election),
            "turnout": round(100 * ballot_count / electorate_count, 1)
            if electorate_count
            else 0,
            "is_open": services.is_open(election),
            "accepted": len(accepted),
            "problems": len(problems),
        },
    )


@election_administrator
@require_http_methods(["GET", "POST"])
@transaction.atomic
def question_edit(request, pk, qid=None):
    election = get_election(pk, request.method == "POST")
    require_draft(election)
    question = (
        get_object_or_404(election.questions, pk=qid) if qid is not None else None
    )
    initial = None
    legacy_quorum_label = ""
    if question:
        threshold = question.voting_quorum
        saved_rule = quorum.question_constitution_rule(
            question.constitutive_quorum,
            question.constitutive_fraction,
            question.minimum_voters,
        )
        if saved_rule is not None:
            if saved_rule.fixed is None and not saved_rule.strict:
                threshold = (
                    f"{saved_rule.numerator}/{saved_rule.denominator}"
                    if saved_rule.numerator
                    else VotingQuorum.FREE
                )
            if threshold not in VotingQuorum.values:
                legacy_quorum_label = saved_rule.label
                threshold = ""
        initial = {
            "text": question.text,
            "kind": question.kind,
            "max_choices": question.max_choices,
            "constitutive_quorum": threshold,
            "deliberation_type": question.deliberation_type or None,
            "deliberative_majority": question.deliberative_majority,
            "majority_override": question.majority_override,
            "majority_basis": question.majority_basis,
            "options": "\n".join(question.choices.values_list("text", flat=True)),
        }
    if request.method == "POST" and request.POST.get("remove") and question:
        if request.POST.get("confirm_remove") != "yes":
            return HttpResponse("Conferma la rimozione del quesito.", status=400)
        question.delete()
        services.audit("quesito_rimosso", actor=request.user, election=election)
        return redirect("voting:detail", pk=pk)
    form = QuestionForm(
        request.POST if request.method == "POST" else None,
        initial=initial,
        election=election,
    )
    if request.method == "POST" and form.is_valid():
        if not question and election.questions.count() >= 30:
            form.add_error(
                None, "Sono ammessi al massimo 30 quesiti per consultazione."
            )
        else:
            question = question or Question(election=election)
            for field in (
                "text",
                "kind",
                "max_choices",
                "voting_quorum",
                "deliberation_type",
                "deliberative_majority",
                "majority_override",
                "majority_basis",
            ):
                setattr(question, field, form.cleaned_data.get(field, ""))
            question.constitutive_quorum = ""
            question.minimum_voters = None
            question.constitutive_fraction = ""
            question.save()
            question.choices.all().delete()
            Choice.objects.bulk_create(
                Choice(question=question, text=label)
                for label in form.cleaned_data["choice_labels"]
            )
            services.audit("quesito_salvato", actor=request.user, election=election)
            return redirect("voting:detail", pk=pk)
    return form_page(
        request,
        "Modifica quesito" if question else "Aggiungi quesito",
        form,
        election,
        remove_question=bool(question),
        question_rules=True,
        question_presets=quorum.DELIBERATION_PRESETS if election.assembly_type else {},
        legacy_quorum_label=legacy_quorum_label,
    )


@election_administrator
@require_GET
def electorate(request, pk):
    return redirect("voting:voters", pk=pk)


@election_administrator
@require_http_methods(["GET", "POST"])
@transaction.atomic
def voter_edit(request, pk, voter_pk=None):
    election = get_election(pk, request.method == "POST")
    require_draft(election)
    voter = (
        get_object_or_404(election.voters, pk=voter_pk)
        if voter_pk is not None
        else ElectionVoter(election=election)
    )
    form = VoterForm(request.POST if request.method == "POST" else None, instance=voter)
    if request.method == "POST" and form.is_valid():
        if voter_pk is None and election.voters.count() >= MAX_ELECTORATE_SIZE:
            form.add_error(
                None,
                f"La platea può contenere al massimo {MAX_ELECTORATE_SIZE} elettori.",
            )
        else:
            form.save()
            services.audit(
                "elettore_salvato",
                actor=request.user,
                election=election,
                voter=voter,
            )
            if (
                election.voters.filter(email__iexact=voter.email)
                .exclude(pk=voter.pk)
                .exists()
            ):
                messages.warning(
                    request,
                    "L’email è condivisa con un altro elettore della consultazione. Verifica il recapito.",
                )
            messages.success(
                request, "Elettore salvato nella platea di questa consultazione."
            )
            return redirect("voting:voters", pk=pk)
    return form_page(
        request,
        "Modifica elettore" if voter_pk is not None else "Aggiungi elettore",
        form,
        election,
        submit_text="Salva elettore",
        back_url=reverse("voting:voters", args=[pk]),
        back_label="Aventi diritto",
    )


@election_administrator
@require_POST
@transaction.atomic
def voter_remove(request, pk, voter_pk):
    election = get_election(pk, lock=True)
    require_draft(election)
    voter = get_object_or_404(election.voters, pk=voter_pk)
    if request.POST.get("confirm") != "yes":
        return HttpResponse(
            "Conferma la rimozione dell’elettore dalla platea.", status=400
        )
    services.audit(
        "elettore_rimosso",
        actor=request.user,
        election=election,
        details={"posizione": voter.pk},
    )
    voter.delete()
    messages.success(request, "Elettore rimosso dalla platea.")
    return redirect("voting:voters", pk=pk)


@election_administrator
@require_http_methods(["GET", "POST"])
@transaction.atomic
def documents(request, pk):
    election = get_election(pk, request.method == "POST")
    require_draft(election)
    if request.method == "POST" and request.POST.get("remove"):
        value = request.POST["remove"]
        if not value.isdecimal():
            raise Http404
        get_object_or_404(election.documents, pk=value).delete()
        services.audit("documento_rimosso", actor=request.user, election=election)
        messages.success(request, "Documento rimosso.")
        return redirect("voting:documents", pk=pk)
    form = DocumentForm(
        request.POST if request.method == "POST" else None, request.FILES or None
    )
    if request.method == "POST" and form.is_valid():
        if election.documents.count() >= 5:
            form.add_error("file", "Sono ammessi al massimo cinque documenti.")
        else:
            form.save(election)
            services.audit("documento_caricato", actor=request.user, election=election)
            messages.success(request, "Documento caricato.")
            return redirect("voting:documents", pk=pk)
    return form_page(
        request,
        "Documenti informativi",
        form,
        election,
        submit_text="Carica PDF",
        multipart=True,
        documents=election.documents.defer("content").order_by("pk"),
    )


def document_response(document):
    return FileResponse(
        BytesIO(bytes(document.content)),
        content_type="application/pdf",
        as_attachment=True,
        filename=document.name,
    )


@election_administrator
@require_GET
def admin_document(request, pk, doc):
    return document_response(
        get_object_or_404(ElectionDocument, pk=doc, election_id=pk)
    )


@election_administrator
@require_GET
def preview(request, pk):
    election = get_election(pk)
    return render(
        request,
        "voting/ballot.html",
        {
            "election": election,
            "preview": True,
            "questions": [
                (question, set()) for question in services.ballot_questions(election)
            ],
        },
    )


TRANSITIONS = {
    "apri": "Apri per votazione",
    "apri-deleghe": "Apri per delega",
    "programma": "Programma l’apertura",
    "chiudi": "Chiudi la consultazione",
    "scrutina": "Pubblica lo scrutinio",
    "annulla": "Annulla la consultazione",
    "elimina": "Elimina la bozza",
    "elimina-pubblicata": "Elimina consultazione",
    "bozza": "Riporta in bozza",
}


def apply_transition(election, action, actor, reason):
    current = election.status
    statuses = Election.Status
    now = services.database_now()
    if action == "apri-deleghe":
        services.open_delegations_locked(election, actor=actor, now=now)
        return "voting:detail"
    if action == "apri":
        services.open_locked(election, actor=actor, now=now)
        return "voting:detail"
    if action == "chiudi":
        services.close_locked(election, actor=actor, now=now)
        return "voting:detail"
    if action == "elimina" and current == statuses.DRAFT:
        services.audit("eliminazione_bozza", actor=actor, election=election)
        election.delete()
        return "voting:dashboard"
    if action == "elimina-pubblicata":
        services.delete_published_locked(election, actor=actor)
        return "voting:dashboard"
    if action == "programma" and current == statuses.DRAFT:
        if (
            election.opens_at is None
            or not now < election.opens_at < election.closes_at
        ):
            raise ValidationError(
                "Imposta un orario futuro di apertura precedente alla scadenza."
            )
        if not election.questions.exists() or not election.voters.exists():
            raise ValidationError("Inserisci almeno un quesito e un elettore.")
        quorum.validate_minimum_voters(election, election.voters.count())
        if election.assembly_type:
            quorum.configuration(election, election.voters.count())
        election.status = statuses.SCHEDULED
    elif action == "bozza" and current == statuses.SCHEDULED:
        election.status = statuses.DRAFT
    elif action == "scrutina" and current == statuses.CLOSED:
        if election.assembly_snapshot:
            verified = election.assembly_verifications.first()
            if verified is None:
                raise ValidationError(
                    "Registra la verifica assembleare prima di pubblicare lo scrutinio."
                )
            data = verified.result
        else:
            data = reports.results_with_outcomes(election)
        election.status = statuses.TALLIED
    elif action == "annulla" and current in {
        statuses.SCHEDULED,
        statuses.DELEGATIONS,
        statuses.OPEN,
        statuses.CLOSED,
    }:
        if not reason:
            raise ValidationError("Indica una motivazione.")
        election.status = statuses.CANCELLED
        election.cancellation_reason = reason
        election.ended_at = election.ended_at or min(now, election.closes_at)
    else:
        raise ValidationError("Operazione non disponibile nello stato attuale.")
    election.save(update_fields=["status", "ended_at", "cancellation_reason"])
    if action == "scrutina":
        enqueue_results(election, actor, now, data)
    services.audit(
        action,
        actor=actor,
        election=election,
        details={"revisione_verifica": data["assembly"]["revision"]}
        if action == "scrutina" and "assembly" in data
        else None,
    )
    return "voting:detail"


@election_administrator
@require_http_methods(["GET", "POST"])
@transaction.atomic
def transition(request, pk, action):
    if action not in TRANSITIONS:
        raise Http404
    if action == "apri-deleghe":
        require_superadmin(request.user)
    election = get_election(pk, request.method == "POST")
    if action == "elimina-pubblicata" and election.status != Election.Status.TALLIED:
        raise Http404
    if request.method == "POST":
        try:
            if request.POST.get("confirm") != "yes":
                raise ValidationError("Conferma l’operazione prima di continuare.")
            reason = request.POST.get("reason", "").strip()
            if len(reason) > 500:
                raise ValidationError(
                    "La motivazione può avere al massimo 500 caratteri."
                )
            destination = apply_transition(election, action, request.user, reason)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        else:
            if action == "elimina-pubblicata":
                messages.success(request, "Consultazione eliminata.")
            elif action == "apri-deleghe":
                messages.success(
                    request,
                    "Deleghe e documenti disponibili. Gli inviti personali sono in coda.",
                )
            elif action == "apri" and election.delegations_opened_at:
                messages.success(
                    request, "Votazione aperta. L’avviso con lo stesso link è in coda."
                )
            if destination == "voting:dashboard":
                return redirect(destination)
            return redirect(destination, pk=pk)
    return render(
        request,
        "voting/transition.html",
        {
            "election": election,
            "action": action,
            "title": TRANSITIONS[action],
        },
    )


def voters_with_participation(election):
    ballots = election.participation_records().filter(voter_id=OuterRef("pk"))
    outgoing = Delegation.objects.filter(
        grantor_id=OuterRef("pk"),
        delegate__election=election,
        state__in=assembly.ACTIVE,
    )
    pending = (
        Delegation.objects.filter(
            delegate_id=OuterRef("pk"),
            grantor__election=election,
            state=Delegation.State.APPROVED,
        )
        .filter(~Exists(assembly.ballots_cast_by_delegate(election)))
        .order_by()
        .values("delegate_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    return election.voters.annotate(
        has_voted=Exists(ballots),
        voted_by_proxy=Exists(ballots.filter(cast_by__isnull=False)),
        outgoing_delegation=Subquery(outgoing.values("state")[:1]),
        pending_delegated_votes=Subquery(pending),
    )


@election_administrator
@require_GET
def voters(request, pk):
    from mailings.models import EmailDelivery, MailKind

    election = get_election(pk)
    search = request.GET.get("q", "").strip()[:150]
    orderings = {
        "surname": ("Cognome e nome · A–Z", ("last_name", "first_name")),
        "surname-desc": ("Cognome e nome · Z–A", ("-last_name", "-first_name")),
        "name": ("Nome e cognome · A–Z", ("first_name", "last_name")),
        "name-desc": ("Nome e cognome · Z–A", ("-first_name", "-last_name")),
        "to-finish": (
            "Stato del voto · da completare prima",
            ("vote_progress", "last_name", "first_name"),
        ),
        "finished": (
            "Stato del voto · completati prima",
            ("-vote_progress", "last_name", "first_name"),
        ),
    }
    sort = request.GET.get("sort", "surname")
    if sort not in orderings:
        sort = "surname"
    deliveries = (
        EmailDelivery.objects.filter(
            voter_id=OuterRef("pk"),
            kind__in=[
                MailKind.INVITE,
                MailKind.RESEND,
                MailKind.EARLY,
                MailKind.VOTE_OPEN,
            ],
        )
        .exclude(campaign__state="preview")
        .order_by("-pk")
    )
    queryset = voters_with_participation(election).annotate(
        mail_state=Subquery(deliveries.values("state")[:1]),
    )
    for term in search.split():
        queryset = queryset.filter(
            Q(first_name__icontains=term)
            | Q(last_name__icontains=term)
            | Q(external_id__icontains=term)
            | Q(email__icontains=term)
            | Q(phone__icontains=term)
        )
    if sort in {"to-finish", "finished"}:
        queryset = queryset.annotate(
            vote_progress=Case(
                When(pending_delegated_votes__gt=0, then=Value(0)),
                When(voted_by_proxy=True, then=Value(5)),
                When(has_voted=True, then=Value(4)),
                When(outgoing_delegation=Delegation.State.APPROVED, then=Value(3)),
                When(outgoing_delegation=Delegation.State.PENDING, then=Value(2)),
                default=Value(1),
            )
        )
    queryset = queryset.order_by(*orderings[sort][1], "pk")
    page = Paginator(queryset, 40).get_page(request.GET.get("page"))
    state_field = EmailDelivery._meta.get_field("state")
    labels = dict(state_field.flatchoices)
    for voter in page:
        voter.mail_label = labels.get(voter.mail_state, "Nessun invio")
    return render(
        request,
        "voting/voters.html",
        {
            "election": election,
            "page": page,
            "search": search,
            "sort": sort,
            "sort_options": [(key, label) for key, (label, _) in orderings.items()],
            "links_available": services.links_available(election),
        },
    )


def whatsapp_number(phone):
    number = re.sub(r"[\s()./\-]", "", phone)
    if number.startswith("+"):
        number = number[1:]
    elif number.startswith("00"):
        number = number[2:]
    elif re.fullmatch(r"3[0-9]{8,9}", number):
        number = "39" + number
    else:
        return None
    return number if re.fullmatch(r"[1-9][0-9]{6,14}", number) else None


@election_administrator
@require_GET
def voter_detail(request, pk, voter_pk):
    from mailings.models import CampaignState, MailKind

    election = get_election(pk)
    voter = get_object_or_404(voters_with_participation(election), pk=voter_pk)
    record = election.participation_records().filter(voter=voter).first()
    last_invite = (
        voter.deliveries.filter(
            kind__in=[
                MailKind.INVITE,
                MailKind.RESEND,
                MailKind.EARLY,
                MailKind.VOTE_OPEN,
            ]
        )
        .exclude(campaign__state=CampaignState.PREVIEW)
        .order_by("-pk")
        .first()
    )
    return render(
        request,
        "voting/voter_detail.html",
        {
            "election": election,
            "voter": voter,
            "last_invite": last_invite,
            "named_vote": reports.ballot_summary(
                services.ballot_questions(election), record.answers
            )
            if record
            and not election.is_secret
            and election.status == Election.Status.TALLIED
            else None,
            "links_available": services.links_available(election),
            "whatsapp_phone": whatsapp_number(voter.phone),
            "local_link": urlsplit(settings.AIM_BASE_URL).hostname
            in {"localhost", "127.0.0.1", "::1"},
        },
    )


@superadmin_required
@require_POST
@transaction.atomic
def link(request, pk, voter_pk):
    election = get_election(pk, lock=True)
    voter = get_object_or_404(election.voters, pk=voter_pk)
    whatsapp_phone = request.POST.get("whatsapp_phone")
    if whatsapp_phone is not None:
        current_phone = whatsapp_number(voter.phone)
        if not current_phone or current_phone != whatsapp_phone:
            return JsonResponse(
                {
                    "error": "Verifica il cellulare nella scheda dell’elettore e riprova."
                },
                status=409,
            )
    try:
        url = services.copy_link(election, voter, request.user)
    except ValidationError as exc:
        return JsonResponse({"error": exc.messages[0]}, status=409)
    result = {"url": url}
    if whatsapp_phone is not None:
        result["whatsapp_url"] = f"https://web.whatsapp.com/send?phone={current_phone}"
    return JsonResponse(result)


@superadmin_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def rotate(request, pk, voter_pk):
    election = get_election(pk, request.method == "POST")
    voter = get_object_or_404(election.voters, pk=voter_pk)
    form = ReasonForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            services.rotate_link(
                election, voter, request.user, form.cleaned_data["reason"]
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(
                request, "Link precedente revocato. Puoi copiare il link attivo."
            )
            return redirect("voting:voters", pk=pk)
    return form_page(
        request,
        "Revoca e rigenera link",
        form,
        election,
        submit_text="Revoca e rigenera link",
        subject=voter,
    )


@election_administrator
@require_http_methods(["GET", "POST"])
@transaction.atomic
def contact(request, pk, voter_pk):
    election = get_election(pk, request.method == "POST")
    if election.status not in {
        Election.Status.DRAFT,
        Election.Status.SCHEDULED,
    } and not services.links_available(election):
        raise Http404
    if services.links_available(election):
        require_superadmin(request.user)
    voter = get_object_or_404(election.voters, pk=voter_pk)
    form = ContactForm(
        request.POST if request.method == "POST" else None,
        initial={"email": voter.email, "phone": voter.phone},
    )
    if request.method == "POST" and form.is_valid():
        changed = []
        for field in ("email", "phone"):
            value = form.cleaned_data[field]
            if getattr(voter, field) != value:
                setattr(voter, field, value)
                changed.append(field)
        if changed:
            voter.save(update_fields=changed)
            services.audit(
                "recapiti_corretti",
                actor=request.user,
                election=election,
                voter=voter,
                details={"campi": changed, "motivo": form.cleaned_data["reason"]},
            )
        messages.success(request, "Recapiti della consultazione salvati.")
        return redirect("voting:voter_detail", pk=pk, voter_pk=voter_pk)
    return form_page(request, "Correggi i recapiti", form, election, subject=voter)


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def access(request):
    if request.method == "GET":
        return render(request, "voting/access.html")
    source = request.META.get("REMOTE_ADDR", "unknown")
    if not services.admit_request("link", source, limit=180, seconds=60):
        return JsonResponse(
            {"error": "Troppi tentativi. Riprova fra un minuto."}, status=429
        )
    token = request.POST.get("token", "")
    if token.startswith(proxy_access.TOKEN_PREFIX):
        binding = proxy_access.decode(token)
        delegation = proxy_access.approved_delegation(binding)
        if delegation is None or (
            delegation.grantor.election.status
            not in {Election.Status.CLOSED, Election.Status.TALLIED}
            and not services.is_open(delegation.grantor.election)
        ):
            return JsonResponse(
                {"error": "Il link per questa delega non è valido o è scaduto."},
                status=404,
            )
        request.session.flush()
        rotate_token(request)
        request.session[proxy_access.SESSION_KEY] = binding
        return JsonResponse(
            {
                "ok": True,
                "ballot_url": reverse("voting:proxy_ballot", args=[delegation.pk]),
            }
        )
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        return JsonResponse({"error": "Il link personale non è valido."}, status=404)
    credential = (
        VotingCredential.objects.filter(digest=services.digest(token))
        .values("voter_id", "voter__election_id", "version")
        .first()
    )
    if credential is None:
        return JsonResponse(
            {"error": "Il link personale non è più valido."}, status=404
        )
    request.session.flush()
    rotate_token(request)
    request.session[SESSION_KEY] = [
        credential["voter_id"],
        credential["voter__election_id"],
        credential["version"],
    ]
    return JsonResponse({"ok": True})


def voter_context(request):
    ids = request.session.get(SESSION_KEY)
    if not ids:
        return None
    voter_id, election_id, version = ids
    return (
        ElectionVoter.objects.select_related("election")
        .filter(
            pk=voter_id,
            election_id=election_id,
            credential__version=version,
        )
        .first()
    )


def ballot_page(
    request,
    voter,
    ballot_record=None,
    answers=None,
    *,
    editing=False,
    error=None,
    status=200,
    delegation=None,
):
    if voter is None:
        return render(request, "voting/unavailable.html", status=403)
    election = voter.election
    if services.is_delegation_phase(election):
        return render(
            request,
            "voting/delegation_phase.html",
            {"election": election, "documents": election.documents.defer("content")},
            status=409 if request.method == "POST" else 200,
        )
    if election.status == Election.Status.TALLIED:
        if delegation is not None:
            participated = (
                ballot_record and ballot_record.cast_by_id == delegation.delegate_id
            )
        else:
            participated = (
                ballot_record
                or election.participation_records().filter(cast_by=voter).exists()
            )
        if participated:
            return render(
                request,
                "voting/voter_results.html",
                {"election": election, **reports.results_with_outcomes(election)},
            )
    if not services.is_open(election):
        return render(request, "voting/unavailable.html", {"election": election})
    if answers is None:
        answers = (
            ballot_record.answers
            if ballot_record
            and editing
            and election.allow_replacement
            and not election.is_secret
            else {}
        )
    actor_id = delegation.delegate_id if delegation else voter.pk
    scoped_link = proxy_access.SESSION_KEY in request.session
    received_delegations = 0
    has_delegations_to_vote = False
    if election.allow_delegations and not scoped_link:
        received = Delegation.objects.filter(
            delegate_id=actor_id,
            grantor__election=election,
            state=Delegation.State.APPROVED,
        )
        received_delegations = received.count()
        if received_delegations:
            has_delegations_to_vote = received.filter(
                ~Exists(assembly.ballots_cast_by_delegate(election))
            ).exists()
    return render(
        request,
        "voting/ballot.html",
        {
            "election": election,
            "voter": voter,
            "delegation": delegation,
            "scoped_link": scoped_link,
            "received_delegations": received_delegations,
            "has_delegations_to_vote": has_delegations_to_vote,
            "presence_gate": election.require_presence
            and not services.has_confirmed_presence(actor_id),
            "delegated_out": delegation is None
            and Delegation.objects.filter(
                grantor=voter,
                state=Delegation.State.APPROVED,
            ).exists(),
            "record": ballot_record,
            "turnout": reports.turnout(election) if ballot_record else None,
            "editing": editing,
            "questions": [
                (question, set(answers.get(str(question.pk), [])))
                for question in services.ballot_questions(election)
            ],
            "documents": election.documents.defer("content"),
            "error": error,
        },
        status=status,
    )


@require_http_methods(["GET", "POST"])
@sensitive_post_parameters()
def ballot(request):
    voter = voter_context(request)
    if voter is None:
        return ballot_page(request, None)
    return voter_ballot(request, voter)


@require_http_methods(["GET", "POST"])
@sensitive_post_parameters()
def proxy_ballot(request, delegation_pk):
    scoped = request.session.get(proxy_access.SESSION_KEY)
    if scoped is not None:
        delegation = proxy_access.approved_delegation(scoped)
        if delegation is None or delegation.pk != delegation_pk:
            raise Http404
        return voter_ballot(request, delegation.grantor, delegation)
    actor = voter_context(request)
    if actor is None:
        return ballot_page(request, None)
    delegation = get_object_or_404(
        Delegation.objects.select_related("grantor__election"),
        pk=delegation_pk,
        delegate=actor,
        grantor__election=actor.election,
        state=Delegation.State.APPROVED,
    )
    return voter_ballot(request, delegation.grantor, delegation)


@sensitive_variables("answers", "data", "payload", "raw", "summary")
def voter_ballot(request, voter, delegation=None):
    current = voter.election.participation_records().filter(voter=voter).first()
    if request.method == "GET" or not services.is_open(voter.election):
        return ballot_page(
            request,
            voter,
            current,
            editing=request.GET.get("modifica") == "1",
            delegation=delegation,
        )
    if (
        delegation is None
        and Delegation.objects.filter(
            grantor=voter, state=Delegation.State.APPROVED
        ).exists()
    ):
        return ballot_page(request, voter, current, status=409)
    binding = (
        request.session.get(SESSION_KEY) or request.session[proxy_access.SESSION_KEY]
    )
    if request.POST.get("action") == "confirm_presence":
        try:
            services.confirm_presence_from_ballot(
                *binding[:3], delegation_id=delegation.pk if delegation else None
            )
        except ValidationError as exc:
            return ballot_page(
                request,
                voter,
                current,
                delegation=delegation,
                error=exc.messages[0],
                status=409,
            )
        if delegation:
            return redirect("voting:proxy_ballot", delegation_pk=delegation.pk)
        return redirect("voting:ballot")
    if voter.election.require_presence and not services.has_confirmed_presence(
        binding[0]
    ):
        return ballot_page(
            request,
            voter,
            current,
            delegation=delegation,
            error="Conferma la tua presenza prima di votare.",
            status=409,
        )
    if current and not voter.election.allow_replacement:
        return ballot_page(
            request,
            voter,
            current,
            error="La sostituzione non è consentita.",
            status=409,
            delegation=delegation,
        )
    questions = services.ballot_questions(voter.election)
    try:
        revision = int(request.POST.get("revision", "-1"))
        if revision != (current.revision if current else 0):
            raise ValidationError(
                "La scheda è cambiata. Compilala di nuovo prima di confermare."
            )
        keys = set(request.POST) - {"revision", "csrfmiddlewaretoken"}
        if any(re.fullmatch(r"q[0-9]+", name) is None for name in keys):
            raise ValidationError("Scheda non valida.")
        raw = {}
        for name in keys:
            values = request.POST.getlist(name)
            raw[name[1:]] = [] if values == [""] else [int(value) for value in values]
        answers = services.validate_answers(questions, raw)
    except (ValueError, ValidationError) as exc:
        error = (
            exc.messages[0]
            if isinstance(exc, ValidationError)
            else "Opzioni non valide."
        )
        return ballot_page(
            request,
            voter,
            current,
            editing=True,
            error=error,
            status=400,
            delegation=delegation,
        )
    data = [binding, str(uuid4()), revision, answers]
    if delegation:
        data.append(delegation.pk)
    payload = signing.dumps(data, salt=SUBMISSION_SALT)
    summary = reports.ballot_summary(questions, answers)
    return render(
        request,
        "voting/review.html",
        {
            "election": voter.election,
            "voter": voter,
            "summary": summary,
            "payload": payload,
            "blank": not answers,
            "replacement": bool(current),
            "delegation": delegation,
            "ballot_url": reverse("voting:proxy_ballot", args=[delegation.pk])
            if delegation
            else reverse("voting:ballot"),
        },
    )


@require_POST
@sensitive_post_parameters()
@sensitive_variables("answers")
def submit(request):
    scoped = request.session.get(proxy_access.SESSION_KEY)
    if scoped is None:
        voter = voter_context(request)
        if voter is None:
            return ballot_page(request, None)
    else:
        delegation = proxy_access.approved_delegation(scoped)
        if delegation is None:
            return render(
                request,
                "voting/unavailable.html",
                {"error": "La delega non è più valida."},
                status=409,
            )
        voter = delegation.grantor
    try:
        session_data, request_id, revision, answers, *proxy = signing.loads(
            request.POST.get("payload", ""),
            salt=SUBMISSION_SALT,
        )
        if session_data != (scoped or request.session[SESSION_KEY]) or (
            scoped is not None and proxy != [scoped[3]]
        ):
            raise signing.BadSignature
        receipt, current = services.record_ballot(
            *session_data[:3],
            request_id,
            revision,
            answers,
            delegation_id=proxy[0] if proxy else None,
        )
    except (signing.BadSignature, ValueError):
        return render(
            request,
            "voting/unavailable.html",
            {"error": "Conferma della scheda non valida."},
            status=400,
        )
    except ValidationError as exc:
        return render(
            request,
            "voting/unavailable.html",
            {
                "election": voter.election,
                "error": exc.messages[0],
            },
            status=409,
        )
    return render(
        request,
        "voting/receipt.html",
        {
            "election": voter.election,
            "receipt": receipt,
            "current": current,
            "turnout": reports.turnout(voter.election),
            "proxy": bool(proxy),
            "return_url": reverse("voting:proxy_ballot", args=[scoped[3]])
            if scoped is not None
            else reverse("voting:assembly" if proxy else "voting:ballot"),
        },
    )


@require_GET
def voter_document(request, pk):
    voter = voter_context(request)
    if voter is None:
        delegation = proxy_access.approved_delegation(
            request.session.get(proxy_access.SESSION_KEY)
        )
        if delegation is not None:
            voter = delegation.grantor
    if not voter or not services.links_available(voter.election):
        raise Http404
    return document_response(
        get_object_or_404(ElectionDocument, pk=pk, election=voter.election)
    )


def tallied_election(pk, *, lock=False):
    queryset = Election.objects.select_for_update() if lock else Election
    return get_object_or_404(queryset, pk=pk, status=Election.Status.TALLIED)


@election_administrator
@require_GET
def results(request, pk):
    election = tallied_election(pk)
    data = reports.results_with_outcomes(election)
    verification = (
        election.assembly_verifications.filter(
            revision=data["assembly"]["revision"]
        ).first()
        if "assembly" in data
        else None
    )
    return render(
        request,
        "voting/results.html",
        {
            "election": election,
            "verification": verification,
            **data,
        },
    )


@election_administrator
@require_http_methods(["GET", "POST"])
@transaction.atomic
def verify_assembly(request, pk):
    election = get_election(pk, request.method == "POST")
    if not election.assembly_snapshot or election.status not in {
        Election.Status.CLOSED,
        Election.Status.TALLIED,
    }:
        raise Http404
    previous = election.assembly_verifications.first()
    tally = reports.tally(election)
    if previous:
        initial = {
            "president": previous.president,
            "reference": previous.reference,
            "present": previous.participation["present"],
            "delegations": [
                item["id"] for item in previous.participation["delegations"]
            ],
        }
    else:
        initial = {
            "present": list(
                VoterPresence.objects.filter(
                    voter__election=election,
                    source=VoterPresence.Source.EXPLICIT,
                ).values_list("voter_id", flat=True)
            )
        }
    form = AssemblyVerificationForm(
        request.POST if request.method == "POST" else None,
        election=election,
        tally=tally,
        previous=previous,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        fields = form.cleaned_data
        revision = previous.revision if previous else 0
        if fields["expected_revision"] != revision:
            form.add_error(None, "La verifica è stata aggiornata. Riapri il modulo.")
        else:
            verification = AssemblyVerification.objects.create(
                election=election,
                revision=revision + 1,
                president=fields["president"],
                reference=fields["reference"],
                correction_reason=fields.get("correction_reason", ""),
                verified_by=request.user,
                verified_at=services.database_now(),
                participation=fields["participation"],
                result=quorum.verified_result(
                    election.assembly_snapshot,
                    fields["participation"],
                    tally,
                    revision + 1,
                ),
            )
            services.audit(
                "rettifica_assembleare" if previous else "verifica_assembleare",
                actor=request.user,
                election=election,
                details={
                    "verifica": verification.pk,
                    "revisione": verification.revision,
                },
            )
            messages.success(request, "Verifica assembleare registrata.")
            return redirect(
                "voting:results"
                if election.status == Election.Status.TALLIED
                else "voting:detail",
                pk=pk,
            )
    history = election.assembly_verifications.defer(
        "participation", "result"
    ).select_related("verified_by")
    return render(
        request,
        "voting/verify_assembly.html",
        {
            "election": election,
            "form": form,
            "previous": previous,
            "history": history,
            "ballots": tally["total"],
            "eligible": election.assembly_snapshot["eligible"],
        },
    )


@election_administrator
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def assembly_report_page(request, pk):
    if request.method == "POST":
        action = request.POST.get("download")
        if action not in {"pdf", "docx", "zip", "save", "preview"}:
            raise Http404
        preview = action == "preview"
        with transaction.atomic():
            election = tallied_election(pk, lock=not preview)
            form = AssemblyMinutesForm(request.POST, election=election)
            valid = form.is_valid()
            if valid:
                fields = form.save(commit=not preview)
                if fields and not preview:
                    services.audit(
                        "dati_verbale_aggiornati",
                        actor=request.user,
                        election=election,
                        details={"campi": fields},
                    )
        if valid:
            if action == "save":
                messages.success(request, "Dati del verbale salvati.")
                return redirect("voting:assembly_report", pk=pk)
            return assembly_report.export(election, kind=action)
    else:
        election = get_election(pk)
        if election.status not in {
            Election.Status.DRAFT,
            Election.Status.CLOSED,
            Election.Status.TALLIED,
        }:
            raise Http404
        form = AssemblyMinutesForm(election=election)
    return render(
        request,
        "voting/assembly_report.html",
        {
            "election": election,
            "form": form,
            "legal_note": assembly_report.LEGAL_NOTE,
        },
    )


@election_administrator
@require_GET
def export(request, pk, kind):
    election = tallied_election(pk)
    if kind == "risultati-csv":
        return reports.aggregate_csv(election)
    if kind == "partecipazione-csv":
        return reports.participation_csv(election)
    if kind == "report-pdf":
        return reports.summary_pdf(election)
    if kind in {
        "rapporto-assembleare-pdf",
        "rapporto-assembleare-docx",
        "rapporto-assembleare-zip",
    }:
        return assembly_report.export(
            election, kind=kind.removeprefix("rapporto-assembleare-")
        )
    raise Http404
