import csv
from collections import Counter
from io import BytesIO
from xml.sax.saxutils import escape

from django.http import FileResponse, HttpResponse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import NamedBallot
from .services import ballot_questions


def tally(election):
    ballots = list(
        NamedBallot.objects.filter(voter__election=election).values_list(
            "answers", flat=True
        )
    )
    rows = []
    for question in ballot_questions(election):
        selections = [answer.get(str(question.pk), []) for answer in ballots]
        counts = Counter(choice_id for selected in selections for choice_id in selected)
        rows.append(
            {
                "question": question.text,
                "answered": sum(bool(selected) for selected in selections),
                "omitted": sum(not selected for selected in selections),
                "choices": [
                    (choice.text, counts[choice.pk])
                    for choice in question.choices.all()
                ],
            }
        )
    return {
        "rows": rows,
        "total": len(ballots),
        "blank": sum(not answer for answer in ballots),
        "eligible": election.voters.count(),
    }


def csv_cell(value):
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n"))
        or value.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠"))
    ):
        return "'" + value
    return value


def csv_response(filename, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    for row in rows:
        writer.writerow(csv_cell(value) for value in row)
    return response


def aggregate_csv(election):
    result = tally(election)
    rows = [
        ["Consultazione", election.title],
        ["Aventi diritto", result["eligible"]],
        ["Schede valide", result["total"]],
        ["Schede bianche", result["blank"]],
        [
            "Quesito",
            "Schede con risposta",
            "Quesito lasciato vuoto",
            "Opzione",
            "Preferenze",
        ],
    ]
    for item in result["rows"]:
        rows.extend(
            [item["question"], item["answered"], item["omitted"], label, count]
            for label, count in item["choices"]
        )
    return csv_response("risultati.csv", rows)


def participation_csv(election):
    ballot_times = dict(
        NamedBallot.objects.filter(voter__election=election).values_list(
            "voter_id", "submitted_at"
        )
    )
    rows = [
        [
            "ID socio",
            "Nome",
            "Cognome",
            "Sezione",
            "Ha votato",
            "Ultima acquisizione (Europa/Roma)",
        ]
    ]
    for voter in election.voters.iterator():
        submitted_at = ballot_times.get(voter.pk)
        rows.append(
            [
                voter.external_id,
                voter.first_name,
                voter.last_name,
                voter.section,
                "Sì" if submitted_at else "No",
                timezone.localtime(submitted_at).isoformat() if submitted_at else "",
            ]
        )
    return csv_response("partecipazione.csv", rows)


def summary_pdf(election):
    data = tally(election)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, title="AIM Voto - Rapporto di scrutinio"
    )
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    ended = (
        timezone.localtime(election.ended_at).strftime("%d/%m/%Y %H:%M %Z")
        if election.ended_at
        else ""
    )
    story = [
        Paragraph("AIM Voto · Rapporto di scrutinio", styles["Title"]),
        Paragraph(escape(election.title), styles["Heading1"]),
        Paragraph(f"Chiusura {ended} (Europe/Rome) · Voto palese", body),
        Paragraph("Riepilogo non firmato a supporto della verbalizzazione.", body),
        Paragraph(
            f"Aventi diritto <b>{data['eligible']}</b> · Schede valide <b>{data['total']}</b>"
            f" · Schede bianche <b>{data['blank']}</b>",
            body,
        ),
    ]
    for row in data["rows"]:
        story.append(Paragraph(escape(row["question"]), styles["Heading2"]))
        story.append(
            Paragraph(
                f"Schede con risposta {row['answered']} su {data['total']} · "
                f"Quesito lasciato vuoto {row['omitted']}",
                body,
            )
        )
        cells = [["Opzione", "Preferenze"]]
        cells.extend(
            [Paragraph(escape(label), body), str(count)]
            for label, count in row["choices"]
        )
        table = Table(cells, colWidths=[350, 85], repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5eff3")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ]
            )
        )
        story.extend([table, Spacer(1, 8)])
    doc.build(story)
    buffer.seek(0)
    return FileResponse(
        buffer,
        content_type="application/pdf",
        as_attachment=True,
        filename="scrutinio.pdf",
    )
