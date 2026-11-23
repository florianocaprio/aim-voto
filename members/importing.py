import csv
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from io import BytesIO, StringIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from voting.services import audit

from .models import ImportBatch, Member

IMPORT_FIELDS = {
    "external_id": "ID socio",
    "first_name": "Nome",
    "last_name": "Cognome",
    "email": "Email",
    "phone": "Cellulare",
    "section": "Sezione",
}
OPTIONAL_FIELDS = {"phone"}
HEADER_ALIASES = {
    "external_id": ("id socio", "id socio esterno", "external id", "codice socio"),
    "first_name": ("nome", "name", "first name"),
    "last_name": ("cognome", "surname", "last name"),
    "email": ("email", "e mail", "posta elettronica"),
    "phone": ("cellulare", "numero cellulare", "telefono", "phone"),
    "section": ("sezione", "section"),
}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_CELL_BYTES = 2 * 1024 * 1024
MAX_XLSX_BYTES = 8 * 1024 * 1024
MAX_XLSX_ENTRIES = 128
MAX_ROWS = 5_000
MAX_COLUMNS = 32
MAX_CELL_LENGTH = 512
PHONE_FORMAT = re.compile(r"\+?[0-9][0-9 ()./\-]{3,31}")
IMPORT_CONFIRM_LOCK = (7396, 1)
STATUS_NEW = "new"
STATUS_UPDATE = "updated"
STATUS_UNCHANGED = "unchanged"


class ImportFormatError(Exception):
    pass


@dataclass
class RowPreview:
    number: int
    values: dict[str, str]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = ""
    previous: dict[str, str] | None = None


@dataclass
class Preview:
    rows: list[RowPreview]
    summary: dict[str, int]
    fingerprint: str


def cell_text(value):
    return unicodedata.normalize("NFC", value).strip()


def read_csv(raw, encoding, separator):
    try:
        data = raw.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise ImportFormatError(
            "Il CSV non corrisponde alla codifica selezionata."
        ) from exc

    reader = csv.reader(StringIO(data, newline=""), delimiter=separator, strict=True)
    try:
        for cells in reader:
            yield {
                "number": reader.line_num,
                "cells": cells,
                "errors": [],
                "non_text": [],
            }
    except csv.Error as exc:
        raise ImportFormatError(f"CSV malformato alla riga {reader.line_num}.") from exc


def verify_xlsx_archive(raw):
    with ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
        if (
            len(entries) > MAX_XLSX_ENTRIES
            or sum(item.file_size for item in entries) > MAX_XLSX_BYTES
        ):
            raise ImportFormatError(
                "Il contenuto decompresso del file XLSX supera i limiti."
            )
        names = [item.filename.casefold() for item in entries]
        if len(set(names)) != len(entries):
            raise ImportFormatError("Il file XLSX contiene voci duplicate.")
        if any(
            name.endswith((".bin", ".vba")) or "macrosheet" in name for name in names
        ):
            raise ImportFormatError("Il file XLSX contiene macro o componenti attivi.")
        for item in entries:
            if item.filename.lower().endswith((".xml", ".rels")):
                root = fromstring(archive.read(item), forbid_dtd=True)
                if item.filename == "[Content_Types].xml":
                    for part in root:
                        content_type = part.get("ContentType", "").lower()
                        if "macro" in content_type or "vba" in content_type:
                            raise ImportFormatError(
                                "Sono accettati soltanto file Excel senza macro."
                            )


def read_xlsx(raw):
    try:
        verify_xlsx_archive(raw)
        workbook = load_workbook(
            BytesIO(raw), read_only=True, data_only=False, keep_links=False
        )
        try:
            if not workbook.worksheets:
                raise ImportFormatError("Nel file XLSX manca un foglio dati.")
            worksheet = workbook.worksheets[0]
            worksheet.reset_dimensions()
            for number, row in enumerate(worksheet.iter_rows(), start=1):
                cells, errors, non_text = [], [], []
                for index, cell in enumerate(row):
                    if cell.data_type == "f":
                        errors.append(f"La colonna {index + 1} contiene una formula.")
                        cells.append("")
                    elif cell.data_type == "e":
                        errors.append(
                            f"La colonna {index + 1} contiene un errore del foglio di calcolo."
                        )
                        cells.append("")
                    elif isinstance(cell.value, str):
                        cells.append(cell.value)
                    else:
                        cells.append("" if cell.value is None else str(cell.value))
                        if cell.value is not None:
                            non_text.append(index)
                yield {
                    "number": number,
                    "cells": cells,
                    "errors": errors,
                    "non_text": non_text,
                }
        finally:
            workbook.close()
    except (
        BadZipFile,
        DefusedXmlException,
        ParseError,
        InvalidFileException,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        OverflowError,
        OSError,
        RuntimeError,
    ) as exc:
        raise ImportFormatError("File XLSX non valido o non supportato.") from exc


def read_upload(upload, encoding, separator, has_header):
    raw = upload.read(MAX_UPLOAD_BYTES + 1)
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ImportFormatError("Il file deve contenere dati e avere al massimo 2 MiB.")
    is_csv = upload.name.lower().endswith(".csv")
    source = read_csv(raw, encoding, separator) if is_csv else read_xlsx(raw)
    headings = []
    records = []
    blank_rows = 0
    width = 0
    cell_bytes = 0
    try:
        for position, record in enumerate(source, start=1):
            if position > MAX_ROWS + int(has_header):
                raise ImportFormatError(
                    f"Il file può contenere al massimo {MAX_ROWS} righe dati."
                )
            cells = [cell_text(value) for value in record["cells"]]
            record["cells"] = cells
            if len(cells) > MAX_COLUMNS:
                raise ImportFormatError(
                    f"Il file può contenere al massimo {MAX_COLUMNS} colonne."
                )
            if any(len(value) > MAX_CELL_LENGTH or "\x00" in value for value in cells):
                raise ImportFormatError(
                    f"Cella troppo lunga o non valida alla riga {record['number']}."
                )
            cell_bytes += sum(len(value.encode("utf-8")) for value in cells)
            if cell_bytes > MAX_CELL_BYTES:
                raise ImportFormatError(
                    "Il contenuto testuale delle celle supera 2 MiB."
                )
            if has_header and position == 1:
                if not any(cells) or record["errors"] or record["non_text"]:
                    raise ImportFormatError(
                        "La prima riga deve contenere intestazioni testuali."
                    )
                headings = cells
                width = len(cells)
                continue
            if not any(cells) and not record["errors"]:
                blank_rows += 1
                continue
            if is_csv:
                if not width:
                    width = len(cells)
                if len(cells) != width:
                    record["errors"].append(
                        f"La riga ha {len(cells)} colonne. La struttura del CSV ne prevede {width}."
                    )
            else:
                width = max(width, len(cells))
            records.append(record)
    finally:
        source.close()
    if not records:
        raise ImportFormatError("Il file non contiene righe da importare.")
    headings += [""] * max(0, width - len(headings))
    return headings, records, blank_rows


def suggested_mapping(headings):
    normalized = [
        re.sub(r"[\s_-]+", " ", label.casefold()).strip() for label in headings
    ]
    result = {}
    for name, aliases in HEADER_ALIASES.items():
        matches = [index for index, label in enumerate(normalized) if label in aliases]
        if len(matches) == 1:
            result[name] = str(matches[0])
    return result


def preview_row(record, mapping):
    cells = record["cells"]
    values = {}
    row = RowPreview(record["number"], values, errors=list(record["errors"]))
    for name, index in mapping.items():
        values[name] = cells[index] if index is not None and index < len(cells) else ""
        label = IMPORT_FIELDS[name]
        model_field = Member._meta.get_field(name)
        if index in record["non_text"]:
            row.errors.append(
                f"{label} deve essere memorizzato come testo nel foglio XLSX."
            )
            continue
        if any(unicodedata.category(char) == "Cc" for char in values[name]):
            row.errors.append(f"{label} contiene caratteri di controllo.")
            continue
        try:
            model_field.clean(values[name], None)
        except ValidationError as exc:
            row.errors.extend(f"{label}. {message}" for message in exc.messages)
    if values["phone"] and not PHONE_FORMAT.fullmatch(values["phone"]):
        row.errors.append(
            "Il cellulare contiene caratteri imprevisti o ha una lunghezza non valida."
        )
    return row


def build_preview(batch):
    rows = [preview_row(record, batch.mapping) for record in batch.records]
    id_counts = Counter(
        row.values["external_id"] for row in rows if row.values["external_id"]
    )
    for row in rows:
        if id_counts[row.values["external_id"]] > 1:
            row.errors.append(
                "ID socio ripetuto nel file. Correggi tutte le occorrenze prima di confermare."
            )
    valid_rows = [row for row in rows if not row.errors]
    existing = Member.objects.in_bulk(
        [row.values["external_id"] for row in valid_rows], field_name="external_id"
    )
    email_keys = [row.values["email"].lower() for row in valid_rows]
    final_owners = defaultdict(set)
    matching_emails = (
        Member.objects.annotate(email_key=Lower("email"))
        .filter(email_key__in=email_keys)
        .values_list("email_key", "external_id")
    )
    for email, external_id in matching_emails:
        if external_id not in existing:
            final_owners[email].add(external_id)
    for row in valid_rows:
        final_owners[row.values["email"].lower()].add(row.values["external_id"])
    shared_examples = {
        email: sorted(owners)[:4]
        for email, owners in final_owners.items()
        if len(owners) > 1
    }

    summary = {
        STATUS_NEW: 0,
        STATUS_UPDATE: 0,
        STATUS_UNCHANGED: 0,
        "errors": len(rows) - len(valid_rows),
        "warnings": 0,
        "blank": batch.blank_rows,
    }
    for row in valid_rows:
        member = existing.get(row.values["external_id"])
        if member:
            row.previous = {name: getattr(member, name) for name in IMPORT_FIELDS}
            if batch.mapping["phone"] is None:
                row.values["phone"] = member.phone
            row.status = (
                STATUS_UNCHANGED if row.previous == row.values else STATUS_UPDATE
            )
        else:
            row.status = STATUS_NEW
        summary[row.status] += 1
        email = row.values["email"].lower()
        if email in shared_examples:
            other_count = len(final_owners[email]) - 1
            examples = [
                value
                for value in shared_examples[email]
                if value != row.values["external_id"]
            ][:3]
            row.warnings.append(
                f"Email condivisa con altri soci ({other_count}, ID: {', '.join(examples)}). Verifica il recapito."
            )
            summary["warnings"] += 1

    snapshot = json.dumps(
        [asdict(row) for row in rows], ensure_ascii=False, sort_keys=True
    )
    fingerprint = salted_hmac(
        f"members.import:{batch.pk}", snapshot, algorithm="sha256"
    ).hexdigest()
    return Preview(rows, summary, fingerprint)


@transaction.atomic
def confirm_import(batch_id, owner, fingerprint):
    batch = (
        ImportBatch.objects.select_for_update()
        .filter(pk=batch_id, owner=owner, expires_at__gt=timezone.now())
        .first()
    )
    if batch is None or batch.completed_at or not batch.mapping:
        return batch, None
    # Serialize roster writes across operators without locking electoral snapshots.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", IMPORT_CONFIRM_LOCK)
    if batch.expires_at <= timezone.now():
        return None, None
    preview = build_preview(batch)
    if preview.summary["errors"] or not constant_time_compare(
        fingerprint, preview.fingerprint
    ):
        return batch, preview
    pending = [
        Member(**row.values) for row in preview.rows if row.status != STATUS_UNCHANGED
    ]
    if pending:
        Member.objects.bulk_create(
            pending,
            update_conflicts=True,
            unique_fields=["external_id"],
            update_fields=[name for name in IMPORT_FIELDS if name != "external_id"],
        )

    batch.completed_at = timezone.now()
    batch.summary = preview.summary
    batch.headings = []
    batch.records = []
    batch.mapping = {}
    batch.save(
        update_fields=["completed_at", "summary", "headings", "records", "mapping"]
    )
    audit("importazione_soci", actor=owner, details=preview.summary)
    return batch, None
