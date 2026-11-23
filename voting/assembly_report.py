from base64 import b64encode
from hashlib import sha256
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from django.http import FileResponse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import reports
from .models import AssemblyQuorum, Delegation, NamedBallot, VoterPresence

PDF_FILENAME = "Rapporto_Assembleare_AIM_VOTO.pdf"
ZIP_FILENAME = "Rapporto_Assembleare_AIM_VOTO.zip"
LEGAL_NOTE = (
    "Il rapporto verifica matematicamente la soglia registrata. La qualificazione giuridica "
    "delle partecipazioni elettroniche e la valida costituzione dell’assemblea restano da accertare "
    "secondo il procedimento concordato con il notaio."
)

INK = colors.HexColor("#15373a")
TINT = colors.HexColor("#e6efed")
BORDER = colors.HexColor("#c6d5d3")


def participation(election):
    presence_ids = set(
        VoterPresence.objects.filter(voter__election=election).values_list(
            "voter_id", flat=True
        )
    )
    ballots = list(
        NamedBallot.objects.filter(voter__election=election).values_list(
            "voter_id", "cast_by_id"
        )
    )
    exercised = {
        (voter, delegate) for voter, delegate in ballots if delegate is not None
    }
    represented_ids = {voter for voter, _ in exercised}
    actors = {
        delegate if delegate is not None else voter for voter, delegate in ballots
    }
    delegations = list(
        Delegation.objects.filter(
            grantor__election=election, delegate__election=election
        )
        .select_related("grantor", "delegate")
        .order_by("pk")
    )
    return {
        "present": len(presence_ids),
        "participants": len(presence_ids | actors),
        "direct": len(ballots) - len(exercised),
        "represented": len(represented_ids),
        "overlap": len(presence_ids & represented_ids),
        "with_proxy": len(presence_ids | represented_ids),
        "approved_unused": sum(
            item.state == Delegation.State.APPROVED
            and (item.grantor_id, item.delegate_id) not in exercised
            for item in delegations
        ),
        "delegations": delegations,
        "exercised": exercised,
    }


def quorum_result(quorum, counts, eligible):
    if quorum is None:
        return None
    counted = (
        counts["with_proxy"]
        if quorum.basis == AssemblyQuorum.Basis.WITH_PROXY
        else counts["present"]
    )
    minimum = quorum.minimum_count(eligible)
    return {"counted": counted, "minimum": minimum, "met": counted >= minimum}


def delegations_csv(counts):
    rows = [
        [
            "Matricola delegante",
            "Nome delegante",
            "Cognome delegante",
            "Matricola delegato",
            "Nome delegato",
            "Cognome delegato",
            "Stato",
            "Esercitata con scheda acquisita",
        ]
    ]
    for item in counts["delegations"]:
        used = (
            item.state == Delegation.State.APPROVED
            and (item.grantor_id, item.delegate_id) in counts["exercised"]
        )
        rows.append(
            [
                item.grantor.external_id,
                item.grantor.first_name,
                item.grantor.last_name,
                item.delegate.external_id,
                item.delegate.first_name,
                item.delegate.last_name,
                item.get_state_display(),
                "Sì" if used else "No",
            ]
        )
    return reports.csv_response("deleghe.csv", rows).content


def supporting_files(election, data, counts):
    return {
        "risultati.csv": reports.aggregate_csv(election, data=data).content,
        "partecipazione.csv": reports.participation_csv(election).content,
        "presenze.csv": reports.presences_csv(election).content,
        "deleghe.csv": delegations_csv(counts),
        "scrutinio.pdf": reports.scrutiny_pdf_buffer(election, data=data).getvalue(),
    }


def date_label(value):
    return (
        timezone.localtime(value).strftime("%d/%m/%Y %H:%M:%S %Z")
        if value
        else "Data assente"
    )


def report_table(rows, widths, *, padding=5):
    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    rules = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, TINT]),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, BORDER),
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]
    table.setStyle(TableStyle(rules))
    return table


def render_pdf(election, data, counts, quorum, hashes, exported_at):
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title="AIM VOTO - Rapporto assembleare",
        author="AIM VOTO",
        leftMargin=48,
        rightMargin=48,
        topMargin=48,
        bottomMargin=48,
    )
    styles = getSampleStyleSheet()
    styles["Title"].textColor = INK
    styles["Heading1"].textColor = INK
    styles["Heading2"].textColor = INK
    body = styles["BodyText"]
    digest_style = ParagraphStyle(
        "Digest", parent=body, fontName="Courier", fontSize=8, leading=10
    )

    def p(text):
        return Paragraph(escape(str(text)), body)

    def footer(canvas, doc):
        width = A4[0]
        canvas.setStrokeColor(BORDER)
        canvas.line(48, 36, width - 48, 36)
        canvas.setFillColor(INK)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(
            48, 23, f"AIM VOTO  |  Consultazione {election.pk}  |  Rapporto assembleare"
        )
        canvas.drawRightString(width - 48, 23, f"Pagina {doc.page}")

    story = [
        Paragraph("Rapporto assembleare", styles["Title"]),
        Paragraph(escape(election.title), styles["Heading1"]),
        p("Consultazione AIM VOTO · Voto palese"),
        Spacer(1, 7),
        report_table(
            [
                ["Riferimento", "Dettaglio (Europe/Rome)"],
                ["Identificativo consultazione", str(election.pk)],
                ["Apertura effettiva", date_label(election.opened_at)],
                ["Chiusura effettiva", date_label(election.ended_at)],
                ["Esportazione", date_label(exported_at)],
            ],
            [200, 282],
        ),
        Spacer(1, 12),
        Paragraph("Partecipazione e rappresentanze", styles["Heading2"]),
        report_table(
            [
                ["Indicatore", "Numero"],
                ["Aventi diritto nella platea", data["eligible"]],
                ["Presenze registrate", counts["present"]],
                ["Partecipanti unici registrati", counts["participants"]],
                ["Schede acquisite direttamente", counts["direct"]],
                [
                    "Rappresentanze esercitate con scheda acquisita",
                    counts["represented"],
                ],
                ["Deleghe approvate senza scheda acquisita", counts["approved_unused"]],
                ["Presenti anche rappresentati (sovrapposizione)", counts["overlap"]],
                [
                    "Presenze + rappresentati, senza doppi conteggi",
                    counts["with_proxy"],
                ],
                ["Schede acquisite complessive", data["total"]],
                ["Schede bianche", data["blank"]],
            ],
            [416, 66],
        ),
        p(
            "I partecipanti unici includono le persone che hanno confermato la presenza "
            "o inviato una scheda, propria o per delega. Una scheda bianca acquisita esercita la delega."
        ),
        Paragraph("Verifica matematica del quorum", styles["Heading2"]),
    ]
    result = quorum_result(quorum, counts, data["eligible"])
    if result is None:
        story.append(p("Quorum non configurato. Verifica matematica non eseguita."))
    else:
        threshold = str(quorum.threshold_numerator) + " persone"
        if quorum.threshold_denominator:
            threshold = f"{quorum.threshold_numerator}/{quorum.threshold_denominator} degli aventi diritto"
        story.extend(
            [
                p(f"Criterio: {quorum.get_basis_display()}"),
                p(
                    f"Soglia registrata: {quorum.get_comparison_display().lower()} {threshold}"
                ),
                p(
                    f"Ultimo salvataggio del criterio: {date_label(quorum.updated_at)} (Europe/Rome)"
                ),
            ]
        )
        if quorum.reference:
            story.append(p(f"Riferimento statutario indicato: {quorum.reference}"))
        outcome = "Soglia raggiunta" if result["met"] else "Soglia non raggiunta"
        story.append(
            Paragraph(
                f"<b>{outcome}</b> - Posizioni conteggiate {result['counted']} · Minimo richiesto {result['minimum']}",
                body,
            )
        )
    story.extend(
        [
            Spacer(1, 6),
            p(LEGAL_NOTE),
            PageBreak(),
            Paragraph("Esito delle votazioni", styles["Heading1"]),
        ]
    )

    for index, item in enumerate(data["rows"], start=1):
        story.extend(
            [
                Paragraph(
                    f"Votazione {index} - {escape(item['question'])}",
                    styles["Heading2"],
                ),
                p(
                    f"Schede con risposta {item['answered']} su {data['total']} · "
                    f"Senza risposta {item['omitted']} · Risposte da schede per delega {item['proxy_answered']}"
                ),
                report_table(
                    [["Opzione", "Preferenze"]]
                    + [[p(label), count] for label, count in item["choices"]],
                    [394, 88],
                    padding=3,
                ),
                Spacer(1, 6),
            ]
        )
    story.append(
        KeepTogether(
            [
                Paragraph("Impronte SHA-256", styles["Heading2"]),
                p(
                    "Il manifesto SHA256SUMS.txt nel pacchetto contiene anche l’impronta del PDF di quella esportazione."
                ),
                report_table(
                    [["File", "Impronta digitale"]]
                    + [
                        [p(name), Paragraph(digest, digest_style)]
                        for name, digest in hashes.items()
                    ],
                    [126, 356],
                    padding=4,
                ),
            ]
        )
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def downloadable(data, *, mimetype, filename):
    response = FileResponse(
        BytesIO(data), content_type=mimetype, as_attachment=True, filename=filename
    )
    digest = b64encode(sha256(data).digest()).decode("ascii")
    response["Content-Digest"] = f"sha-256=:{digest}:"
    return response


def export(election, *, package):
    exported_at = timezone.now()
    data = reports.tally(election)
    counts = participation(election)
    quorum = AssemblyQuorum.objects.filter(election=election).first()
    files = supporting_files(election, data, counts)
    hashes = {name: sha256(content).hexdigest() for name, content in files.items()}
    pdf = render_pdf(election, data, counts, quorum, hashes, exported_at)
    if not package:
        return downloadable(pdf, mimetype="application/pdf", filename=PDF_FILENAME)
    files[PDF_FILENAME] = pdf
    hashes[PDF_FILENAME] = sha256(pdf).hexdigest()
    buffer = BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr(
            "SHA256SUMS.txt",
            "".join(f"{digest}  {name}\n" for name, digest in hashes.items()),
        )
    return downloadable(
        buffer.getvalue(), mimetype="application/zip", filename=ZIP_FILENAME
    )
