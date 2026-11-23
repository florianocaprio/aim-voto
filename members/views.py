import csv
from datetime import timedelta
from io import BytesIO

from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from config.auth import staff_required

from .demo import DEMO_MEMBERS
from .forms import ConfirmForm, MappingForm, UploadForm
from .importing import (
    IMPORT_FIELDS,
    ImportFormatError,
    build_preview,
    confirm_import,
    read_upload,
)
from .models import ImportBatch

UPLOAD_TTL = timedelta(minutes=30)


def active_batches(request):
    return ImportBatch.objects.filter(owner=request.user, expires_at__gt=timezone.now())


@never_cache
@staff_required
@require_http_methods(["GET", "POST"])
def upload(request):
    form = (
        UploadForm(request.POST, request.FILES)
        if request.method == "POST"
        else UploadForm()
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            headings, records, blank_rows = read_upload(
                data["file"], data["encoding"], data["separator"], data["has_header"]
            )
        except ImportFormatError as exc:
            form.add_error("file", str(exc))
        else:
            now = timezone.now()
            ImportBatch.objects.filter(expires_at__lte=now).delete()
            batch = ImportBatch.objects.create(
                owner=request.user,
                expires_at=now + UPLOAD_TTL,
                headings=headings,
                records=records,
                blank_rows=blank_rows,
            )
            return redirect("members:mapping", batch_id=batch.pk)
    return render(request, "members/upload.html", {"form": form})


@never_cache
@staff_required
@require_http_methods(["GET", "POST"])
def mapping(request, batch_id):
    if request.method == "POST":
        with transaction.atomic():
            batch = get_object_or_404(
                active_batches(request).select_for_update(), pk=batch_id
            )
            if batch.completed_at:
                return redirect("members:preview", batch_id=batch.pk)
            form = MappingForm(batch.headings, request.POST, mapping=batch.mapping)
            if form.is_valid():
                batch.mapping = form.cleaned_data
                batch.save(update_fields=["mapping"])
                return redirect("members:preview", batch_id=batch.pk)
    else:
        batch = get_object_or_404(active_batches(request), pk=batch_id)
        if batch.completed_at:
            return redirect("members:preview", batch_id=batch.pk)
        form = MappingForm(batch.headings, mapping=batch.mapping)
    return render(
        request,
        "members/mapping.html",
        {"form": form, "batch": batch, "row_count": len(batch.records)},
    )


@never_cache
@staff_required
@require_http_methods(["GET", "POST"])
def preview(request, batch_id):
    batch = get_object_or_404(active_batches(request), pk=batch_id)
    if batch.completed_at:
        return render(
            request, "members/complete.html", {"batch": batch, "summary": batch.summary}
        )
    if not batch.mapping:
        return redirect("members:mapping", batch_id=batch.pk)

    status = 200
    changed = False
    form = ConfirmForm(request.POST) if request.method == "POST" else None
    if form is not None and form.is_valid():
        batch, result = confirm_import(
            batch_id, request.user, form.cleaned_data["fingerprint"]
        )
        if batch is None:
            raise Http404
        if batch.completed_at:
            return redirect("members:preview", batch_id=batch_id)
        if result is None:
            return redirect("members:mapping", batch_id=batch_id)
        form = ConfirmForm(initial={"fingerprint": result.fingerprint})
        changed = True
        status = 409
    else:
        result = build_preview(batch)
        if form is None:
            form = ConfirmForm(initial={"fingerprint": result.fingerprint})
        else:
            status = 400
    return render(
        request,
        "members/preview.html",
        {
            "batch": batch,
            "preview": result,
            "summary": result.summary,
            "form": form,
            "changed": changed,
        },
        status=status,
    )


def csv_download(filename, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(IMPORT_FIELDS.values())
    writer.writerows(rows)
    return response


@staff_required
@require_GET
def template_csv(request):
    return csv_download("modello-soci.csv", [])


@staff_required
@require_GET
def demo_csv(request):
    return csv_download(
        "soci-dimostrativi.csv",
        ([row[name] for name in IMPORT_FIELDS] for row in DEMO_MEMBERS),
    )


@staff_required
@require_GET
def template_xlsx(request):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Soci"
    sheet.append(list(IMPORT_FIELDS.values()))
    sheet.freeze_panes = "A2"
    for number, name in enumerate(IMPORT_FIELDS, start=1):
        column = sheet.column_dimensions[get_column_letter(number)]
        column.width = max(16, len(IMPORT_FIELDS[name]) + 4)
        column.number_format = "@"
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="modello-soci.xlsx"'
    return response
