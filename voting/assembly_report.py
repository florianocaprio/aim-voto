import json
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

from . import assembly_docx, reports
from .models import AssemblyQuorum, Delegation, VoterPresence

PDF_FILENAME = "Verbale_Assembleare_AIM_VOTO.pdf"
WORD_FILENAME = "Verbale_Assembleare_AIM_VOTO.docx"
WORD_MIMETYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ZIP_FILENAME = "Verbale_Assembleare_AIM_VOTO.zip"
LEGAL_NOTE = (
    "Il verbale riporta la verifica matematica delle soglie previste dalla consultazione. La qualificazione giuridica "
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
        election.participation_records().values_list("voter_id", "cast_by_id")
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
    files = {
        "risultati.csv": reports.aggregate_csv(election, data=data).content,
        reports.participation_filename(election): reports.participation_csv(
            election
        ).content,
        "presenze.csv": reports.presences_csv(election).content,
        "deleghe.csv": delegations_csv(counts),
        "scrutinio.pdf": reports.scrutiny_pdf_buffer(election, data=data).getvalue(),
    }
    if "assembly" in data:
        verifications = election.assembly_verifications.filter(
            revision__lte=data["assembly"]["revision"]
        ).values(
            "revision",
            "president",
            "reference",
            "correction_reason",
            "verified_by_id",
            "verified_at",
            "participation",
            "result",
        )
        files["verifica-assembleare.json"] = json.dumps(
            {
                "configurazione": election.assembly_snapshot,
                "apertura": election.opened_at,
                "chiusura": election.ended_at,
                "matricole": dict(election.voters.values_list("pk", "external_id")),
                "verifiche": list(verifications),
            },
            default=str,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
    return files


def date_label(value):
    return (
        timezone.localtime(value).strftime("%d/%m/%Y %H:%M:%S %Z")
        if value
        else "Data assente"
    )


def assembly_quorum_details(election, data, counts, legacy_quorum):
    if "assembly" in data:
        verification = election.assembly_verifications.get(
            revision=data["assembly"]["revision"]
        )
        return "Verifica assembleare", [
            *reports.assembly_outcome_lines(data),
            f"Presidente {verification.president} · Attestazione {verification.reference}",
            f"Verifica registrata il {date_label(verification.verified_at)}",
        ]
    if data["quorum"] is not None:
        configured = data["quorum"]
        if configured["minimum"]:
            state = "Raggiunto" if configured["met"] else "Non raggiunto"
            return "Quorum della consultazione", [
                f"{configured['label']} · Posizioni conteggiate {configured['counted']} · Minimo {configured['minimum']} · {state}"
            ]
    elif legacy_quorum is not None and not any(
        row.get("quorum") for row in data["rows"]
    ):
        result = quorum_result(legacy_quorum, counts, data["eligible"])
        threshold = str(legacy_quorum.threshold_numerator) + " persone"
        if legacy_quorum.threshold_denominator:
            threshold = f"{legacy_quorum.threshold_numerator}/{legacy_quorum.threshold_denominator} degli aventi diritto"
        lines = [
            f"Criterio {legacy_quorum.get_basis_display()}",
            f"Soglia registrata {legacy_quorum.get_comparison_display().lower()} {threshold}",
            f"Ultimo salvataggio del criterio {date_label(legacy_quorum.updated_at)} (Europe/Rome)",
        ]
        if legacy_quorum.reference:
            lines.append(f"Riferimento statutario indicato {legacy_quorum.reference}")
        outcome = "Soglia raggiunta" if result["met"] else "Soglia non raggiunta"
        lines.append(
            f"{outcome} - Posizioni conteggiate {result['counted']} · Minimo richiesto {result['minimum']}"
        )
        return "Criterio assembleare storico", lines
    return "", []


def minutes_content(election, data, counts, legacy_quorum, exported_at):
    questions = []
    for index, item in enumerate(data["rows"], start=1):
        outcome = {"approved": "Approvata", "rejected": "Non approvata"}.get(
            item.get("outcome"), item.get("outcome_label", "")
        )
        summary = (
            f"Schede con risposta {item['answered']} su {data['total']} · "
            f"Senza risposta {item['omitted']}"
        )
        if not election.is_secret:
            summary += f" · Risposte da schede per delega {item['proxy_answered']}"
        questions.append(
            {
                "title": f"Votazione {index} - {item['question']}",
                "outcome": outcome.upper() if outcome else "",
                "summary": summary,
                "notes": reports.deliberation_outcome_lines(item, include_outcome=False),
                "choices": item["choices"],
            }
        )
    signing_date = election.minutes_signing_date
    return {
        "intro": (
            "Il presente verbale dà conto della partecipazione e dello scrutinio "
            f"della consultazione svolta su AIM VOTO con voto {election.get_mode_display().lower()}."
        ),
        "references": [
            ["Riferimento", "Dettaglio (Europe/Rome)"],
            ["Identificativo consultazione", str(election.pk)],
            ["Apertura effettiva", date_label(election.opened_at)],
            ["Chiusura effettiva", date_label(election.ended_at)],
            ["Esportazione", date_label(exported_at)],
        ],
        "participation": [
            ["Indicatore", "Numero"],
            ["Aventi diritto nella platea", data["eligible"]],
            ["Presenze registrate", counts["present"]],
            ["Partecipanti unici registrati", counts["participants"]],
            ["Schede acquisite direttamente", counts["direct"]],
            ["Rappresentanze esercitate con scheda acquisita", counts["represented"]],
            ["Deleghe approvate senza scheda acquisita", counts["approved_unused"]],
            ["Presenti anche rappresentati (sovrapposizione)", counts["overlap"]],
            ["Presenze + rappresentati, senza doppi conteggi", counts["with_proxy"]],
            ["Schede acquisite complessive", data["total"]],
            ["Schede bianche", data["blank"]],
        ],
        "participation_note": (
            "I partecipanti unici includono le persone che hanno confermato la presenza "
            "o inviato una scheda, propria o per delega. Una scheda bianca acquisita esercita la delega."
        ),
        "quorum": assembly_quorum_details(election, data, counts, legacy_quorum),
        "legal_note": LEGAL_NOTE,
        "questions": questions,
        "signing_details": ", ".join(
            filter(
                None,
                (
                    election.minutes_signing_city,
                    signing_date.strftime("%d/%m/%Y") if signing_date else "",
                ),
            )
        ),
    }


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


def render_pdf(election, content, hashes):
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title="AIM VOTO - Verbale assembleare",
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
    discussion_style = ParagraphStyle(
        "Discussione", parent=body, spaceAfter=3 * body.leading
    )
    digest_style = ParagraphStyle(
        "Digest", parent=body, fontName="Courier", fontSize=8, leading=10
    )
    outcome_style = ParagraphStyle(
        "Esito",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=INK,
        spaceAfter=6,
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
            48, 23, f"AIM VOTO  |  Consultazione {election.pk}  |  Verbale assembleare"
        )
        canvas.drawRightString(width - 48, 23, f"Pagina {doc.page}")

    story = [
        Paragraph("VERBALE ASSEMBLEARE", styles["Title"]),
        Paragraph(escape(election.title), styles["Heading1"]),
    ]
    for heading, text, style in (
        ("Ordine del giorno", election.minutes_agenda, body),
        ("Discussione", election.minutes_discussion, discussion_style),
    ):
        if text.strip():
            story.append(Paragraph(heading, styles["Heading2"]))
            story.append(
                Paragraph("<br/>".join(escape(line) for line in text.splitlines()), style)
            )
    story += [
        p(content["intro"]),
        Spacer(1, 7),
        report_table(content["references"], [200, 282]),
        Spacer(1, 12),
        Paragraph("Partecipazione e rappresentanze", styles["Heading2"]),
        report_table(content["participation"], [416, 66]),
        p(content["participation_note"]),
    ]
    quorum_title, quorum_lines = content["quorum"]
    if quorum_title:
        story.append(Paragraph(quorum_title, styles["Heading2"]))
        if quorum_title == "Criterio assembleare storico":
            story.extend(p(line) for line in quorum_lines[:-1])
            outcome, details = quorum_lines[-1].split(" - ", 1)
            story.append(Paragraph(f"<b>{outcome}</b> - {escape(details)}", body))
        else:
            story.extend(p(line) for line in quorum_lines)
    story.extend(
        [
            Spacer(1, 6),
            p(content["legal_note"]),
            PageBreak(),
            Paragraph("Esito delle votazioni", styles["Heading1"]),
        ]
    )

    for question in content["questions"]:
        intro = [Paragraph(escape(question["title"]), styles["Heading2"])]
        if question["outcome"]:
            intro.append(Paragraph(escape(question["outcome"]), outcome_style))
        intro.append(p(question["summary"]))
        story.extend(
            [
                KeepTogether(intro),
                *(p(line) for line in question["notes"]),
                report_table(
                    [["Opzione", "Preferenze"]]
                    + [[p(label), count] for label, count in question["choices"]],
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

    def signature_label(role, name):
        label = f"{role}<br/><b>{escape(name)}</b>" if name else role
        return Paragraph(label, body)

    signatures = Table(
        [
            [
                signature_label("Firma del presidente", election.minutes_president),
                "",
                signature_label("Firma del segretario", election.minutes_secretary),
            ],
            ["", "", ""],
        ],
        colWidths=[228, 26, 228],
        rowHeights=[None, 34],
        hAlign="LEFT",
    )
    signatures.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, 0), "TOP"),
                ("LINEBELOW", (0, 1), (0, 1), 0.6, INK),
                ("LINEBELOW", (2, 1), (2, 1), 0.6, INK),
            ]
        )
    )
    signature_heading = [
        Spacer(1, 16),
        Paragraph("Sottoscrizione del verbale", styles["Heading2"]),
    ]
    if content["signing_details"]:
        signature_heading.append(p(content["signing_details"]))
    story.append(KeepTogether([*signature_heading, signatures]))
    story.extend(
        reports.voting_appendix(
            election,
            styles,
            482,
            title="Appendice A - Aventi diritto al voto",
            eligible_only=True,
        )
    )
    if not election.is_secret:
        story.extend(
            reports.voting_appendix(
                election,
                styles,
                482,
                title="Appendice B - Votanti e scelte per quesito",
            )
        )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def downloadable(data, *, mimetype, filename, as_attachment=True):
    response = FileResponse(
        BytesIO(data), content_type=mimetype, as_attachment=as_attachment, filename=filename
    )
    digest = b64encode(sha256(data).digest()).decode("ascii")
    response["Content-Digest"] = f"sha-256=:{digest}:"
    return response


def export(election, *, kind):
    exported_at = timezone.now()
    data = reports.results_with_outcomes(election)
    counts = participation(election)
    legacy_quorum = AssemblyQuorum.objects.filter(election=election).first()
    files = supporting_files(election, data, counts)
    hashes = {name: sha256(content).hexdigest() for name, content in files.items()}
    content = minutes_content(election, data, counts, legacy_quorum, exported_at)
    if kind == "docx":
        return downloadable(
            assembly_docx.render(election, content, hashes),
            mimetype=WORD_MIMETYPE,
            filename=WORD_FILENAME,
        )
    pdf = render_pdf(election, content, hashes)
    if kind != "zip":
        preview = kind == "preview"
        return downloadable(
            pdf,
            mimetype="application/pdf",
            filename=f"Anteprima_{PDF_FILENAME}" if preview else PDF_FILENAME,
            as_attachment=not preview,
        )
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
