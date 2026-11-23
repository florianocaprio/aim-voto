from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods

from config.auth import superadmin_required

from .models import Election, ElectionAdministrator
from .services import audit


class ScopeForm(forms.Form):
    GLOBAL = "global"
    SELECTED = "election"
    scope = forms.ChoiceField(label="Accesso amministrativo", initial=GLOBAL)
    elections = forms.ModelMultipleChoiceField(
        label="Consultazioni da assegnare",
        queryset=Election.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, account=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.creating = account is None
        self.fields["elections"].label_from_instance = lambda election: (
            f"{election.title} · #{election.pk} · {election.get_status_display()}"
        )
        self.fields["scope"].choices = [
            (self.GLOBAL, "Tutte le consultazioni"),
            (self.SELECTED, "Consultazioni selezionate"),
        ]
        if account is not None:
            assignment = getattr(account, "election_administration", None)
            if assignment is not None:
                self.initial["scope"] = self.SELECTED
                self.initial["elections"] = list(
                    assignment.elections.values_list("pk", flat=True)
                )
            self.fields[
                "elections"
            ].help_text = "Deseleziona tutte le consultazioni per revocare l’accesso."

    def clean(self):
        data = super().clean()
        if (
            self.creating
            and data.get("scope") == self.SELECTED
            and "elections" in data
            and not data["elections"]
        ):
            self.add_error(
                "elections", "Seleziona almeno una consultazione da assegnare."
            )
        return data

    def save(self, actor, account, action):
        scope = self.cleaned_data["scope"]
        elections = []
        if scope == self.GLOBAL:
            ElectionAdministrator.objects.filter(user=account).delete()
        else:
            selected_ids = [election.pk for election in self.cleaned_data["elections"]]
            elections = list(
                Election.objects.select_for_update()
                .filter(pk__in=selected_ids)
                .order_by("pk")
            )
            if len(elections) != len(selected_ids):
                raise ValidationError(
                    "Una consultazione non è più disponibile. Rivedi la selezione."
                )
            assignment, _ = ElectionAdministrator.objects.get_or_create(user=account)
            assignment.elections.set(elections)
        audit(
            action,
            actor=actor,
            election=elections[0] if len(elections) == 1 else None,
            details={
                "account_id": account.pk,
                "scope": scope,
                "election_ids": [election.pk for election in elections],
            },
        )


def staff_accounts():
    return get_user_model().objects.filter(is_staff=True).order_by("username")


@superadmin_required
@require_GET
def index(request):
    return render(
        request,
        "voting/administrators.html",
        {
            "accounts": staff_accounts()
            .select_related("election_administration")
            .prefetch_related("election_administration__elections")
        },
    )


@sensitive_post_parameters("password1", "password2")
@superadmin_required
@require_http_methods(["GET", "POST"])
def create(request):
    data = request.POST if request.method == "POST" else None
    form = UserCreationForm(data)
    scope_form = ScopeForm(data)
    if data is not None:
        account_valid, scope_valid = form.is_valid(), scope_form.is_valid()
        if account_valid and scope_valid:
            try:
                with transaction.atomic():
                    account = form.save(commit=False)
                    account.is_staff = True
                    account.save()
                    scope_form.save(request.user, account, "amministratore_creato")
            except ValidationError as exc:
                scope_form.add_error("elections", exc)
            except IntegrityError:
                form.add_error(
                    None,
                    "Impossibile creare il profilo. Verifica il nome utente e la consultazione.",
                )
            else:
                messages.success(request, "Profilo amministrativo creato.")
                return redirect("voting:administrators")
    return render(
        request,
        "voting/administrator_form.html",
        {"account_form": form, "scope_form": scope_form},
    )


@superadmin_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def edit(request, user_id):
    accounts = staff_accounts().filter(is_superuser=False)
    if request.method == "POST":
        accounts = accounts.select_for_update()
    account = get_object_or_404(accounts, pk=user_id)
    form = ScopeForm(
        request.POST if request.method == "POST" else None, account=account
    )
    if request.method == "POST" and form.is_valid():
        try:
            form.save(request.user, account, "accesso_amministratore_aggiornato")
        except ValidationError as exc:
            form.add_error("elections", exc)
        else:
            messages.success(request, "Accesso amministrativo aggiornato.")
            return redirect("voting:administrators")
    return render(
        request,
        "voting/administrator_form.html",
        {"account": account, "scope_form": form},
    )
