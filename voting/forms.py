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

from . import quorum
from .models import (
    AssemblyKind,
    Convocation,
    Delegation,
    DeliberationKind,
    DeliberativeMajority,
    Election,
    ElectionDocument,
    ElectionVoter,
    Question,
    QuorumBasis,
    VotingQuorum,
)


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


class ElectionForm(forms.ModelForm):
    opens_at = RomeDateTimeField(
        required=False,
        label="Apertura programmata (ora di Roma)",
        help_text="Lascia vuoto per aprire manualmente.",
    )
    closes_at = RomeDateTimeField(label="Scadenza (ora di Roma)")
    convocation = forms.TypedChoiceField(
        label="Convocazione",
        choices=Convocation.choices,
        coerce=int,
        required=False,
        empty_value=Convocation.FIRST,
        initial=Convocation.FIRST,
    )
    voting_quorum = forms.ChoiceField(
        label="Quorum della votazione",
        choices=[("", "Quorum da configurare"), *VotingQuorum.choices],
        required=False,
        initial=VotingQuorum.FREE,
        help_text="Per le consultazioni senza tipo di assemblea. La soglia si calcola sugli aventi diritto e si verifica sulle schede acquisite.",
    )

    class Meta:
        model = Election
        fields: ClassVar = [
            "title",
            "description",
            "mode",
            "opens_at",
            "closes_at",
            "voting_quorum",
            "assembly_type",
            "convocation",
            "total_members",
            "allow_custom_quorum",
            "constitutive_override",
            "allow_replacement",
            "allow_delegations",
            "require_presence",
        ]
        labels: ClassVar = {
            "title": "Titolo",
            "description": "Descrizione",
            "mode": "Modalità di voto",
            "assembly_type": "Tipo di assemblea",
            "convocation": "Convocazione",
            "total_members": "Totale degli associati alla data di riferimento",
            "allow_custom_quorum": "Autorizza quorum personalizzati",
            "constitutive_override": "Soglia costitutiva personalizzata",
            "allow_replacement": "Consenti la sostituzione del voto",
            "allow_delegations": "Consenti il voto per delega",
            "require_presence": "Richiedi conferma di presenza prima del voto",
        }
        widgets: ClassVar = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts: ClassVar = {
            "require_presence": "Con le deleghe abilitate, il socio può scegliere una delega diretta al posto del voto personale.",
            "mode": (
                "Nel voto palese la scheda è associata al socio. Nel voto segreto "
                "si registra la partecipazione e si pubblicano solo risultati aggregati."
            ),
            "allow_replacement": "Seleziona per consentire modifiche fino alla chiusura. Con la casella vuota, il voto è definitivo dopo la conferma.",
            "assembly_type": "Scegli Ordinaria o Straordinaria per applicare i preset assembleari. Gli aventi diritto sono quelli nell’elenco della consultazione.",
            "constitutive_override": "Vuoto per il preset. Inserisci una frazione, per esempio 2/3 o >1/2.",
        }

    def clean_closes_at(self):
        value = self.cleaned_data["closes_at"]
        if value <= timezone.now():
            raise ValidationError("Scegli una scadenza futura.")
        return value

    def clean(self):
        data = super().clean()
        if not data.get("assembly_type"):
            return data
        if not data.get("total_members"):
            self.add_error("total_members", "Indica il totale degli associati.")
        override = data.get("constitutive_override")
        if override:
            if not data.get("allow_custom_quorum"):
                self.add_error(
                    "constitutive_override", "Autorizza i quorum personalizzati."
                )
            else:
                try:
                    quorum.parse_rule(override, QuorumBasis.ELIGIBLE)
                except ValidationError as error:
                    self.add_error("constitutive_override", error)
        return data


class QuestionForm(forms.Form):
    text = forms.CharField(label="Testo del quesito", max_length=500)
    kind = forms.ChoiceField(
        label="Tipo",
        choices=Question.Kind.choices,
        help_text="Con la variante «Senza risposta», il socio può saltare la mozione. La scheda non entra nel conteggio delle risposte a quel quesito.",
    )
    deliberation_type = forms.ChoiceField(
        label="Tipologia della deliberazione",
        choices=[
            ("", "Automatica in base al tipo di quesito"),
            *DeliberationKind.choices,
        ],
        required=False,
    )
    constitutive_quorum = forms.ChoiceField(
        label="Quorum del quesito",
        required=False,
        choices=[
            (VotingQuorum.FREE, "Libero"),
            *((value, value) for value in VotingQuorum.values[1:]),
        ],
        help_text="Frazione degli aventi diritto. Contano le schede con risposta, comprese le astensioni.",
    )
    deliberative_majority = forms.ChoiceField(
        label="Criterio deliberativo",
        required=False,
        choices=[
            (DeliberativeMajority.VOTES_CAST, "Voti espressi"),
            *DeliberativeMajority.choices[1:],
        ],
        help_text="Voti espressi confronta favorevoli e contrari. La maggioranza assoluta conta anche gli astenuti.",
    )
    majority_override = forms.CharField(
        label="Frazione della maggioranza qualificata",
        max_length=14,
        required=False,
        help_text="Per la maggioranza qualificata inserisci una frazione, ad esempio 2/3.",
    )
    majority_basis = forms.ChoiceField(
        label="Denominatore della maggioranza qualificata",
        choices=[("", "Seleziona il denominatore"), *QuorumBasis.choices],
        required=False,
    )
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

    def __init__(self, *args, election=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.election = election
        self.is_assembly = bool(election and election.assembly_type)
        if not self.is_assembly:
            self.fields.pop("deliberation_type")
        if not self.initial.get("constitutive_quorum"):
            self.initial["constitutive_quorum"] = (
                election.voting_quorum
                if election and not self.is_assembly and election.voting_quorum
                else VotingQuorum.FREE
            )
        if not self.initial.get("deliberative_majority"):
            matter = self.initial.get("deliberation_type") if self.is_assembly else None
            preset = quorum.DELIBERATION_PRESETS.get(
                matter, quorum.DELIBERATION_PRESETS[DeliberationKind.ORDINARY]
            )
            for field, value in zip(
                ("deliberative_majority", "majority_override", "majority_basis"),
                preset,
                strict=True,
            ):
                self.initial[field] = value

    def clean_rules(self, cleaned, motion):
        cleaned["voting_quorum"] = (
            cleaned.get("constitutive_quorum") or self.initial["constitutive_quorum"]
        )
        matter = cleaned.get("deliberation_type") or (
            DeliberationKind.ORDINARY if motion else DeliberationKind.ADVISORY
        )
        mode = (
            (cleaned.get("deliberative_majority") or "")
            if motion and matter != DeliberationKind.ADVISORY
            else ""
        )
        fraction = (
            cleaned.get("majority_override", "")
            if mode == DeliberativeMajority.QUALIFIED
            else ""
        )
        basis = (
            cleaned.get("majority_basis", "")
            if mode == DeliberativeMajority.QUALIFIED
            else ""
        )
        try:
            if self.is_assembly:
                cleaned["deliberation_type"] = matter
                if (
                    matter in quorum.EXTRAORDINARY_MATTERS
                    and self.election.assembly_type != AssemblyKind.EXTRAORDINARY
                ):
                    self.add_error(
                        "deliberation_type",
                        "Questo quesito richiede un’assemblea straordinaria.",
                    )
                if (mode, fraction, basis) == quorum.DELIBERATION_PRESETS.get(matter):
                    mode, fraction, basis = "", "", ""
                if (
                    matter == DeliberationKind.CUSTOM or mode
                ) and not self.election.allow_custom_quorum:
                    raise ValidationError(
                        "Autorizza i quorum personalizzati nelle impostazioni dell’assemblea.",
                    )
                _, rule = quorum.deliberation_rule(matter, mode, fraction, basis)
            elif motion:
                mode = mode or DeliberativeMajority.VOTES_CAST
                rule = quorum.majority_rule(mode, fraction, basis)
            else:
                rule = None
        except ValidationError as error:
            self.add_error("deliberative_majority", error)
        else:
            cleaned.update(
                deliberative_majority=mode,
                majority_override=fraction,
                majority_basis=basis,
            )
            if rule is not None and not motion:
                self.add_error(
                    "kind",
                    "Scegli Favorevole / Contrario / Astenuto per una deliberazione.",
                )

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        if not kind or "options" not in cleaned or "max_choices" not in cleaned:
            return cleaned
        motion = kind in {Question.Kind.MOTION, Question.Kind.MOTION_REQUIRED}
        self.clean_rules(cleaned, motion)
        if motion:
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
        if motion or kind == Question.Kind.SINGLE:
            cleaned["max_choices"] = 1
        elif cleaned["max_choices"] > len(options):
            self.add_error("max_choices", "Il limite supera il numero delle opzioni.")
        cleaned["choice_labels"] = options
        return cleaned


class AssemblyMinutesForm(forms.Form):
    president = forms.CharField(
        label="Nome e cognome del presidente", max_length=180, required=False
    )
    secretary = forms.CharField(
        label="Nome e cognome del segretario", max_length=180, required=False
    )
    signing_city = forms.CharField(
        label="Città della firma", max_length=180, required=False
    )
    signing_date = forms.DateField(
        label="Data della firma",
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    agenda = forms.CharField(
        label="Ordine del giorno",
        max_length=6000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    discussion = forms.CharField(
        label="Discussione",
        max_length=20000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
    )

    def __init__(self, *args, election, **kwargs):
        super().__init__(*args, **kwargs)
        self.election = election
        self.initial.update(
            {name: getattr(election, f"minutes_{name}") for name in self.fields}
        )

    def save(self, *, commit=True):
        fields = []
        for name in self.changed_data:
            # An already open form can omit fields introduced by an update.
            if name in self.data:
                field = f"minutes_{name}"
                setattr(self.election, field, self.cleaned_data[name])
                fields.append(field)
        if fields and commit:
            self.election.save(update_fields=fields)
        return fields


class AssemblyVerificationForm(forms.Form):
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    president = forms.CharField(label="Presidente dell’assemblea", max_length=180)
    reference = forms.CharField(
        label="Riferimento al verbale o all’attestazione", max_length=300
    )
    present = forms.ModelMultipleChoiceField(
        label="Presenti riconosciuti in proprio",
        queryset=ElectionVoter.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    delegations = forms.ModelMultipleChoiceField(
        label="Rappresentanze riconosciute",
        queryset=Delegation.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    correction_reason = forms.CharField(
        label="Motivazione della rettifica",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    attest = forms.BooleanField(
        label="Registro l’attestazione del Presidente sulla partecipazione, sulle deleghe e sulla legittimazione al voto."
    )

    def __init__(self, *args, election, tally, previous=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tally = tally
        self.eligible = election.assembly_snapshot["eligible"]
        self.initial["expected_revision"] = previous.revision if previous else 0
        self.fields["present"].queryset = election.voters.all()
        self.fields["present"].label_from_instance = lambda v: (
            f"{v.first_name} {v.last_name} · {v.external_id}"
        )
        self.fields["delegations"].queryset = Delegation.objects.filter(
            grantor__election=election,
            delegate__election=election,
            state=Delegation.State.APPROVED,
        ).select_related("grantor", "delegate")
        self.fields["delegations"].label_from_instance = lambda d: (
            f"{d.grantor.first_name} {d.grantor.last_name} ({d.grantor.external_id}) "
            f"rappresentato da {d.delegate.first_name} {d.delegate.last_name} ({d.delegate.external_id})"
        )
        if previous is None:
            self.fields.pop("correction_reason")
        for index, row in enumerate(tally["rows"], start=1):
            key = f"participants_{row['id']}"
            self.fields[key] = forms.IntegerField(
                label=f"Partecipanti aventi diritto al quesito {index} · {row['question']}",
                min_value=0,
                max_value=self.eligible,
                required=False,
                initial=previous.participation["by_question"].get(str(row["id"]))
                if previous
                else None,
                help_text=f"Schede con risposta {row['answered']}. Lascia vuoto per usare i partecipanti riconosciuti sopra.",
            )

    def clean(self):
        cleaned = super().clean()
        if "present" not in cleaned or "delegations" not in cleaned:
            return cleaned
        present = {item.pk for item in cleaned["present"]}
        delegations = list(cleaned["delegations"])
        if any(item.delegate_id not in present for item in delegations):
            self.add_error(
                "delegations", "Il delegato deve essere tra i presenti riconosciuti."
            )
        represented = {item.grantor_id for item in delegations}
        recognized = len(present | represented)
        by_question = {}
        for row in self.tally["rows"]:
            key = f"participants_{row['id']}"
            if key not in cleaned:
                continue
            number = cleaned[key] if cleaned[key] is not None else recognized
            if number < row["answered"]:
                self.add_error(
                    key,
                    "Indica almeno tante posizioni quante sono le schede con risposta.",
                )
            else:
                by_question[str(row["id"])] = number
        cleaned["participation"] = {
            "present": sorted(present),
            "delegations": [
                {
                    "id": item.pk,
                    "grantor": item.grantor_id,
                    "delegate": item.delegate_id,
                }
                for item in delegations
            ],
            "recognized": recognized,
            "represented_only": len(represented - present),
            "by_question": by_question,
        }
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
    file = forms.FileField(
        label="Documento PDF (massimo 3 MB)",
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,application/pdf"}),
    )

    def clean_file(self):
        file = self.cleaned_data["file"]
        if file.size > 3 * 1024 * 1024:
            raise ValidationError("Carica un file PDF valido di massimo 3 MB.")
        try:
            content = file.read()
        except (OSError, ValueError) as exc:
            raise ValidationError(
                "Impossibile leggere il documento. Seleziona di nuovo il file."
            ) from exc
        if not content.startswith(b"%PDF-"):
            raise ValidationError("Carica un file PDF valido di massimo 3 MB.")
        return file.name[:150], content

    def save(self, election):
        name, content = self.cleaned_data["file"]
        return ElectionDocument.objects.create(
            election=election, name=name, content=content
        )


class ReceivedDelegationForm(forms.Form):
    file = forms.FileField(
        label="Domanda compilata (PDF o JPG, massimo 3 MB)",
        max_length=150,
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.jpg,.jpeg"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 3 * 1024 * 1024:
            raise ValidationError("Il documento supera il limite di 3 MB.")
        try:
            content = upload.read()
        except (OSError, ValueError) as exc:
            raise ValidationError(
                "Impossibile leggere il documento. Seleziona di nuovo il file."
            ) from exc
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
                SyntaxError,
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
