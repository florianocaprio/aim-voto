from types import SimpleNamespace

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from config.auth import superadmin_required
from voting.access import election_administrator
from voting.models import Election
from voting.services import is_delegation_phase, is_open

from .campaigns import (
    INVITATION_TEMPLATE_STATES,
    cancel_campaign,
    confirm_campaign,
    create_preview,
    recipients,
    render_body,
    save_invitation_template,
    update_interval,
)
from .forms import CampaignForm, IntervalForm
from .models import CampaignState, DeliveryState, MailKind, ReminderCampaign

PREVIEW_LINK = "[link personale aggiunto dal sistema]"


def _editor(request, election_id, kind, voter_id=None):
    election = get_object_or_404(Election, pk=election_id)
    voter = None
    if voter_id is not None:
        voter = get_object_or_404(election.voters, pk=voter_id)
    invitation = kind == MailKind.INVITE
    editing_template = invitation and election.status in INVITATION_TEMPLATE_STATES
    saving_template = editing_template or (
        invitation and request.POST.get("action") == "save_template"
    )
    template = getattr(election, "invitation_template", None) if invitation else None
    sections = (
        election.voters.order_by("section").values_list("section", flat=True).distinct()
    )
    form = CampaignForm(
        request.POST if request.method == "POST" else None,
        kind=kind,
        sections=None if editing_template else sections,
        template=template,
    )
    if request.method == "POST" and form.is_valid():
        try:
            if saving_template:
                save_invitation_template(election.pk, request.user, form.cleaned_data)
            else:
                campaign = create_preview(
                    election.pk,
                    request.user,
                    kind,
                    form.cleaned_data,
                    voter_id=voter_id,
                )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            if saving_template:
                messages.success(request, "Modello di invito salvato.")
                return redirect("mailings:invite", election.pk)
            return redirect("mailings:campaign", election.pk, campaign.pk)
    model = ReminderCampaign(election=election, kind=kind, **form.initial)
    sample_voter = voter or SimpleNamespace(first_name="Nome", last_name="Cognome")
    return render(
        request,
        "mailings/editor.html",
        {
            "election": election,
            "kind": kind,
            "kind_label": "Anteprima email di invito"
            if editing_template
            else kind.label,
            "voter": voter,
            "form": form,
            "open": is_open(election)
            or (kind == MailKind.RESEND and is_delegation_phase(election)),
            "editing_template": editing_template,
            "sample_subject": model.subject,
            "sample_body": render_body(model, sample_voter, PREVIEW_LINK),
        },
    )


@superadmin_required
@require_http_methods(["GET", "POST", "HEAD"])
def invite(request, election_id):
    return _editor(request, election_id, MailKind.INVITE)


@superadmin_required
@require_http_methods(["GET", "POST", "HEAD"])
def remind(request, election_id):
    return _editor(request, election_id, MailKind.REMINDER)


@superadmin_required
@require_http_methods(["GET", "POST", "HEAD"])
def resend(request, election_id, voter_id):
    return _editor(request, election_id, MailKind.RESEND, voter_id)


@election_administrator
@require_safe
def history(request, election_id):
    election = get_object_or_404(Election, pk=election_id)
    campaigns = (
        election.campaigns.exclude(state=CampaignState.PREVIEW)
        .select_related("author")
        .annotate(
            total=Count("deliveries"),
            queued=Count(
                "deliveries", filter=Q(deliveries__state=DeliveryState.QUEUED)
            ),
            accepted=Count(
                "deliveries", filter=Q(deliveries__state=DeliveryState.ACCEPTED)
            ),
        )
    )
    return render(
        request,
        "mailings/history.html",
        {
            "election": election,
            "campaigns": campaigns,
            "voting_open": is_open(election),
            "interval_form": IntervalForm(
                initial={"hours": election.reminder_interval_hours}
            ),
        },
    )


@election_administrator
@require_safe
def campaign_detail(request, election_id, campaign_id):
    election = get_object_or_404(Election, pk=election_id)
    campaign = get_object_or_404(
        election.campaigns.select_related("author"), pk=campaign_id
    )
    preview = campaign.state == CampaignState.PREVIEW
    if preview and not request.user.is_superuser:
        raise Http404()
    rows = list(
        campaign.deliveries.select_related("voter").order_by(
            "voter__last_name", "voter__first_name", "pk"
        )
    )
    counts = {key: 0 for key in DeliveryState.values}
    for row in rows:
        counts[row.state] += 1
    sample = next(
        (row.voter for row in rows if row.state == DeliveryState.PREVIEW), None
    )
    if preview and campaign.kind == MailKind.REMINDER:
        by_id = {
            voter.pk: voter
            for voter in recipients(election, campaign.kind).filter(
                pk__in=[row.voter_id for row in rows]
            )
        }
        for row in rows:
            row.last_reminder = by_id[row.voter_id].last_reminder
            row.accepted_reminders = by_id[row.voter_id].accepted_reminders
    return render(
        request,
        "mailings/campaign.html",
        {
            "election": election,
            "campaign": campaign,
            "preview": preview,
            "is_reminder": campaign.kind == MailKind.REMINDER,
            "sample_body": render_body(campaign, sample, PREVIEW_LINK)
            if sample
            else "",
            "rows": rows,
            "counts": counts,
        },
    )


@superadmin_required
@require_POST
def confirm(request, election_id, campaign_id):
    get_object_or_404(Election, pk=election_id)
    try:
        confirm_campaign(election_id, campaign_id, request.user)
    except ValidationError as error:
        messages.error(request, error.messages[0])
    return redirect("mailings:campaign", election_id, campaign_id)


@election_administrator
@require_POST
def cancel(request, election_id, campaign_id):
    get_object_or_404(Election, pk=election_id)
    cancel_campaign(election_id, campaign_id, request.user)
    return redirect("mailings:campaign", election_id, campaign_id)


@election_administrator
@require_POST
def interval(request, election_id):
    get_object_or_404(Election, pk=election_id)
    form = IntervalForm(request.POST)
    if form.is_valid():
        update_interval(
            election_id,
            request.user,
            form.cleaned_data["hours"],
            form.cleaned_data["reason"],
        )
        messages.success(request, "Intervallo fra solleciti aggiornato.")
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect("mailings:history", election_id)
