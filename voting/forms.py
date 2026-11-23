import unicodedata
from datetime import datetime
from io import BytesIO
from pathlib import PurePath
from typing import ClassVar

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from members.importing import PHONE_FORMAT, cell_text

from .models import AssemblyQuorum, Election, ElectionDocument, ElectionVoter, Question


class RomeDateTimeField(forms.DateTimeField):
    widget = forms.DateTimeInput(
        format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}
    )

    def to_python(self, value):
        if value and isinstance(value, str):
            try:
                parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
            except ValueError as exc:
                raise ValidationError(
                    "Inserisci una data e un’ora locali valide."
                ) from exc
            # Django rejects wall times that are repeated or absent at the DST change.
            return super().to_python(parsed)
        return super().to_python(value)


class ElectionModeSelect(forms.Select):
    def optgroups(self, name, value, attrs=None):
        groups = super().optgroups(name, value, attrs)
        # Render the planned mode without adding it to the model field's accepted choices.
        index = len(groups)
        option = self.create_option(
            name, "secret", "Segreto (non ancora disponibile)", False, index
        )
        option["attrs"]["disabled"] = True
        return [*groups, (None, [option], index)]


class ElectionForm(forms.ModelForm):
    opens_at = RomeDateTimeField(
        required=False,
        label="Apertura programmata (ora di Roma)",
        help_text="Lascia vuoto per aprire manualmente.",
    )
    closes_at = RomeDateTimeField(label="Scadenza (ora di Roma)")

    class Meta:
        model = Election
        fields: ClassVar = [
            "title",
            "description",
            "mode",
            "opens_at",
            "closes_at",
            "allow_replacement",
            "allow_delegations",
            "require_presence",
        ]
        labels: ClassVar = {
            "title": "Titolo",
            "description": "Descrizione",
            "mode": "Modalità di voto",
            "allow_replacement": "Consenti la sostituzione del voto",
            "allow_delegations": "Consenti il voto per delega",
            "require_presence": "Richiedi conferma di presenza prima del voto",
        }
        widgets: ClassVar = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "mode": ElectionModeSelect,
        }
        help_texts: ClassVar = {
            "require_presence": "Con le deleghe abilitate, il socio può scegliere una delega diretta al posto del voto personale.",
            "mode": (
                "Nel voto palese la scheda è associata al socio. "
                "Il voto segreto richiede un motore che separi l’identità dalla scelta."
            ),
        }

    def clean_closes_at(self):
        value = self.cleaned_data["closes_at"]
        if value <= timezone.now():
            raise ValidationError("Scegli una scadenza futura.")
        return value


class AssemblyQuorumForm(forms.ModelForm):
    class Meta:
        model = AssemblyQuorum
        fields: ClassVar = [
            "basis",
            "comparison",
            "threshold_numerator",
            "threshold_denominator",
            "reference",
        ]
        labels: ClassVar = {
            "basis": "Partecipazioni da conteggiare",
            "comparison": "Condizione",
            "threshold_numerator": "Soglia in persone o numeratore",
            "threshold_denominator": "Denominatore della frazione (facoltativo)",
            "reference": "Riferimento statutario (facoltativo)",
        }
        help_texts: ClassVar = {
            "basis": "Se includi le rappresentanze esercitate, ogni posizione viene conteggiata una sola volta.",
            "threshold_numerator": "Per una soglia di due terzi, inserisci 2 qui e 3 nel denominatore.",
            "threshold_denominator": "Lascia vuoto se lo statuto indica un numero di persone.",
        }

    def clean(self):
        cleaned = super().clean()
        denominator = cleaned.get("threshold_denominator")
        numerator = cleaned.get("threshold_numerator")
        if numerator and denominator and numerator > denominator:
            self.add_error(
                "threshold_denominator",
                "Il denominatore deve essere almeno pari al numeratore.",
            )
        return cleaned


class QuestionForm(forms.Form):
    text = forms.CharField(label="Testo del quesito", max_length=500)
    kind = forms.ChoiceField(label="Tipo", choices=Question.Kind.choices)
    options = forms.CharField(
        label="Opzioni, una per riga",
        required=False,
        max_length=4500,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Per le mozioni le opzioni sono Favorevole, Contrario e Astenuto.",
    )
    max_choices = forms.IntegerField(
        label="Massimo di preferenze", min_value=1, max_value=40, initial=1
    )

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        if not kind or "options" not in cleaned or "max_choices" not in cleaned:
            return cleaned
        if kind == Question.Kind.MOTION:
            options = ["Favorevole", "Contrario", "Astenuto"]
        else:
            options = [
                option.strip()
                for option in cleaned["options"].splitlines()
                if option.strip()
            ]
            if not 2 <= len(options) <= 40 or any(
                len(option) > 200 for option in options
            ):
                self.add_error(
                    "options",
                    "Inserisci da 2 a 40 opzioni, ciascuna di massimo 200 caratteri.",
                )
            elif len({option.casefold() for option in options}) != len(options):
                self.add_error("options", "Le opzioni devono essere distinte.")
        if kind in {Question.Kind.MOTION, Question.Kind.SINGLE}:
            cleaned["max_choices"] = 1
        elif cleaned["max_choices"] > len(options):
            self.add_error("max_choices", "Il limite supera il numero delle opzioni.")
        cleaned["choice_labels"] = options
        return cleaned


class VoterForm(forms.ModelForm):
    email = forms.EmailField(label="Email")

    class Meta:
        model = ElectionVoter
        fields: ClassVar = [
            "first_name",
            "last_name",
            "external_id",
            "email",
            "phone",
            "section",
        ]
        labels: ClassVar = {
            "first_name": "Nome",
            "last_name": "Cognome",
            "external_id": "Matricola",
            "phone": "Cellulare (facoltativo)",
            "section": "Sezione (facoltativa)",
        }

    def clean(self):
        cleaned = super().clean()
        for field, value in list(cleaned.items()):
            if any(unicodedata.category(char) == "Cc" for char in value):
                self.add_error(field, "Il valore contiene caratteri di controllo.")
            else:
                cleaned[field] = cell_text(value)
        phone = cleaned.get("phone")
        if phone and not PHONE_FORMAT.fullmatch(phone):
            self.add_error("phone", "Inserisci un numero di cellulare valido.")
        matricola = cleaned.get("external_id")
        if (
            matricola
            and ElectionVoter.objects.filter(
                election=self.instance.election,
                external_id=matricola,
            )
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            self.add_error(
                "external_id", "La matricola è già presente in questa consultazione."
            )
        return cleaned


class DocumentForm(forms.Form):
    file = forms.FileField(label="Documento PDF (massimo 3 MB)")

    def clean_file(self):
        file = self.cleaned_data["file"]
        if file.size > 3 * 1024 * 1024 or file.read(5) != b"%PDF-":
            raise ValidationError("Carica un file PDF valido di massimo 3 MB.")
        file.seek(0)
        return file

    def save(self, election):
        file = self.cleaned_data["file"]
        return ElectionDocument.objects.create(
            election=election, name=file.name[:150], content=file.read()
        )


class ReceivedDelegationForm(forms.Form):
    first_name = forms.CharField(label="Nome del delegante", max_length=100)
    last_name = forms.CharField(label="Cognome del delegante", max_length=100)
    external_id = forms.CharField(label="Matricola del delegante", max_length=64)
    file = forms.FileField(
        label="Domanda compilata (PDF o JPG, massimo 3 MB)",
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.jpg,.jpeg"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 3 * 1024 * 1024:
            raise ValidationError("Il documento supera il limite di 3 MB.")
        content = upload.read()
        extension = PurePath(upload.name).suffix.lower()
        if extension == ".pdf" and content.startswith(b"%PDF-"):
            return upload.name[:150], "application/pdf", content
        if extension in {".jpg", ".jpeg"}:
            try:
                with Image.open(BytesIO(content), formats=["JPEG"]) as image:
                    if image.width * image.height <= 25_000_000:
                        image.verify()
                        return upload.name[:150], "image/jpeg", content
            except (
                UnidentifiedImageError,
                Image.DecompressionBombError,
                OSError,
                ValueError,
            ):
                pass
        raise ValidationError("Allega un file PDF o JPG valido.")


class ContactForm(forms.Form):
    email = forms.EmailField(label="Email", required=False)
    phone = forms.CharField(label="Cellulare", required=False, max_length=32)
    reason = forms.ChoiceField(
        label="Motivazione",
        choices=[
            ("errore", "Correzione di un errore"),
            ("aggiornamento", "Aggiornamento confermato dal socio"),
            ("recapito_non_valido", "Recapito non più utilizzabile"),
            ("altro", "Altra correzione verificata"),
        ],
    )


class ReasonForm(forms.Form):
    reason = forms.ChoiceField(
        label="Motivazione",
        choices=[
            ("esposizione", "Possibile esposizione del link"),
            ("destinatario", "Invio a un destinatario errato"),
            ("richiesta_socio", "Richiesta verificata del socio"),
            ("altro", "Altra necessità operativa"),
        ],
    )
    confirm = forms.BooleanField(label="Confermo l’operazione")
