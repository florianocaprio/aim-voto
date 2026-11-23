import re

from django import forms

from .models import MailKind

DEFAULT_MESSAGES = {
    MailKind.INVITE: (
        "Invito a partecipare",
        "Ti invitiamo a partecipare alla consultazione di Angeli in Moto ODV.",
    ),
    MailKind.REMINDER: (
        "Promemoria per la consultazione",
        (
            "Ti ricordiamo che puoi ancora partecipare alla consultazione. "
            "Se hai già votato, non è necessaria alcuna azione."
        ),
    ),
    MailKind.RESEND: (
        "Reinvio del link personale",
        "Come richiesto, ti inviamo nuovamente il tuo link personale per partecipare alla consultazione.",
    ),
}

PRIVATE_LINK_OR_TOKEN = re.compile(
    r"https?://|(?<![\w-])[A-Za-z0-9_-]{43}(?![\w-])", re.IGNORECASE
)


class CampaignForm(forms.Form):
    subject = forms.CharField(label="Oggetto", max_length=140)
    body = forms.CharField(
        label="Messaggio",
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Nome, titolo, scadenza, assistenza e link personale vengono aggiunti a ogni email.",
    )
    section = forms.ChoiceField(label="Sezione", required=False)

    def __init__(self, *args, kind, sections, template=None, **kwargs):
        subject, body = DEFAULT_MESSAGES[kind]
        if template is not None:
            subject, body = template.subject, template.body
        super().__init__(*args, initial={"subject": subject, "body": body}, **kwargs)
        if kind == MailKind.RESEND or sections is None:
            del self.fields["section"]
        else:
            self.fields["section"].choices = [
                ("", "Tutte le sezioni"),
                *[(s, s) for s in sections if s],
            ]

    def clean_subject(self):
        subject = self.cleaned_data["subject"]
        if any(
            ord(character) < 32 for character in subject
        ) or PRIVATE_LINK_OR_TOKEN.search(subject):
            raise forms.ValidationError(
                "Usa un oggetto su una sola riga, senza collegamenti o credenziali."
            )
        return subject

    def clean_body(self):
        body = self.cleaned_data["body"]
        if PRIVATE_LINK_OR_TOKEN.search(body):
            raise forms.ValidationError(
                "Rimuovi i collegamenti e le credenziali dal messaggio."
            )
        return body


class IntervalForm(forms.Form):
    hours = forms.IntegerField(
        label="Intervallo minimo in ore", min_value=1, max_value=720
    )
    reason = forms.CharField(label="Motivazione della modifica", max_length=250)

    def clean_reason(self):
        reason = self.cleaned_data["reason"]
        if PRIVATE_LINK_OR_TOKEN.search(reason):
            raise forms.ValidationError(
                "Inserisci una motivazione senza collegamenti o credenziali."
            )
        return reason
