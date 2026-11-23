from pathlib import PurePath

from django import forms

from .importing import (
    IMPORT_FIELDS,
    MAX_UPLOAD_BYTES,
    OPTIONAL_FIELDS,
    suggested_mapping,
)


class UploadForm(forms.Form):
    file = forms.FileField(
        label="File CSV o XLSX",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.xlsx"}),
        help_text="Massimo 2 MiB, 5.000 righe dati e 32 colonne. Nei file XLSX viene letto il primo foglio.",
    )
    has_header = forms.BooleanField(
        label="La prima riga contiene le intestazioni", required=False, initial=True
    )
    separator = forms.ChoiceField(
        label="Separatore CSV",
        choices=[(";", "Punto e virgola"), (",", "Virgola"), ("\t", "Tabulazione")],
        help_text="Per un file XLSX questo valore non viene usato.",
    )
    encoding = forms.ChoiceField(
        label="Codifica CSV",
        choices=[
            ("utf-8-sig", "UTF-8 (anche con BOM)"),
            ("cp1252", "Windows-1252"),
            ("latin-1", "ISO-8859-1"),
        ],
        help_text="Per un file XLSX questo valore non viene usato.",
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if PurePath(upload.name).suffix.lower() not in {".csv", ".xlsx"}:
            raise forms.ValidationError("Scegli un file .csv o .xlsx.")
        if upload.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("Il file supera il limite di 2 MiB.")
        return upload


class MappingForm(forms.Form):
    def __init__(self, headings, *args, mapping=None, **kwargs):
        initial = {
            name: "" if index is None else str(index)
            for name, index in (mapping or {}).items()
        }
        super().__init__(
            *args, initial=initial or suggested_mapping(headings), **kwargs
        )
        options = [
            (str(index), f"Colonna {index + 1}" + (f" — {heading}" if heading else ""))
            for index, heading in enumerate(headings)
        ]
        for name, label in IMPORT_FIELDS.items():
            self.fields[name] = forms.ChoiceField(
                label=label,
                choices=[("", "Non associata")] + options,
                required=name not in OPTIONAL_FIELDS,
                help_text=(
                    "Se non associ questa colonna, i cellulari già registrati vengono conservati."
                    if name == "phone"
                    else None
                ),
            )

    def clean(self):
        data = super().clean()
        used = set()
        for value in data.values():
            if value and value in used:
                raise forms.ValidationError("Associa ogni colonna a un solo campo.")
            if value:
                used.add(value)
        return {name: int(value) if value else None for name, value in data.items()}


class ConfirmForm(forms.Form):
    fingerprint = forms.CharField(
        widget=forms.HiddenInput, min_length=64, max_length=64
    )
    confirmed = forms.BooleanField(
        label="Ho verificato le righe e gli eventuali avvisi."
    )
