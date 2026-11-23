import csv
from collections import Counter
from io import BytesIO
from xml.sax.saxutils import escape

from django.http import FileResponse, HttpResponse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import quorum as quorum_rules
from . import secret_ballots
from .models import Question, VoterPresence, VotingQuorum
from .services import ballot_questions


def turnout(election):
    return {
        "voted": election.participation_records().count(),
        "eligible": election.voters.count(),
    }


def tally(election):
    ballots = (
        secret_ballots.for_tally(election)
        if election.is_secret
        else [
            (answers, cast_by is not None)
            for answers, cast_by in election.participation_records().values_list(
                "answers", "cast_by_id"
            )
        ]
    )
    rows = []
    for question in ballot_questions(election):
        selections = [answer.get(str(question.pk), []) for answer, _ in ballots]
        counts = Counter(choice_id for selected in selections for choice_id in selected)
        rows.append(
            {
                "id": question.pk,
                "question": question.text,
                "kind": question.kind,
                "voting_quorum": question.voting_quorum,
                "constitutive_quorum": question.constitutive_quorum,
                "constitutive_fraction": question.constitutive_fraction,
                "minimum_voters": question.minimum_voters,
                "deliberative_majority": question.deliberative_majority,
                "majority_override": question.majority_override,
                "majority_basis": question.majority_basis,
                "answered": sum(bool(selected) for selected in selections),
                "omitted": sum(not selected for selected in selections),
                "proxy_answered": sum(
                    bool(selected) and is_proxy
                    for selected, (_, is_proxy) in zip(selections, ballots, strict=True)
                ),
                "choices": [
                    (choice.text, counts[choice.pk])
                    for choice in question.choices.all()
                ],
            }
        )
    return {
        "rows": rows,
        "total": len(ballots),
        "blank": sum(not answer for answer, _ in ballots),
        "eligible": election.voters.count(),
    }


def participation_quorum(rule, eligible, counted):
    if rule not in VotingQuorum.values:
        return None
    if rule == VotingQuorum.FREE:
        minimum = 0
    else:
        numerator, denominator = map(int, rule.split("/"))
        minimum = (eligible * numerator + denominator - 1) // denominator
    return {
        "label": VotingQuorum(rule).label,
        "met": counted >= minimum,
        "counted": counted,
        "minimum": minimum,
    }


def results_with_outcomes(election):
    if election.assembly_snapshot:
        verification = election.assembly_verifications.first()
        if verification is not None:
            return verification.result
    data = tally(election)
    eligible = data["eligible"]
    quorum = participation_quorum(election.voting_quorum, eligible, data["total"])
    quorum_failed = quorum is not None and not quorum["met"]
    for row in data["rows"]:
        rule = quorum_rules.question_constitution_rule(
            row["constitutive_quorum"],
            row["constitutive_fraction"],
            row["minimum_voters"],
        )
        question_quorum = (
            quorum_rules.question_quorum_outcome(rule, eligible, row["answered"])
            if rule is not None
            else participation_quorum(
                row["voting_quorum"] or election.voting_quorum,
                eligible,
                row["answered"],
            )
        )
        row["quorum"] = question_quorum
        row["outcome"] = "unavailable"
        mode = row["deliberative_majority"]
        if mode:
            choices = dict(row["choices"])
            favorable, against = (
                choices.get("Favorevole", 0),
                choices.get("Contrario", 0),
            )
            majority = quorum_rules.majority_rule(
                mode, row["majority_override"], row["majority_basis"]
            )
            row.update(
                matter="Deliberazione",
                participants=data["total"],
                favorable=favorable,
                against=against,
                abstained=choices.get("Astenuto", 0),
                no_choice=row["omitted"],
                deliberative=quorum_rules.deliberation_outcome(
                    majority,
                    mode,
                    not quorum_failed
                    and (question_quorum is None or question_quorum["met"]),
                    eligible=eligible,
                    members=election.total_members,
                    participants=data["total"],
                    favorable=favorable,
                    against=against,
                ),
            )
        if quorum_failed:
            row["outcome_label"] = "Esito non valido per mancato quorum della votazione"
        elif question_quorum is not None and not question_quorum["met"]:
            row["outcome_label"] = "Quorum del quesito non raggiunto"
        elif mode:
            if row["deliberative"]["approved"]:
                row["outcome"], row["outcome_label"] = "approved", "Approvata"
            else:
                row["outcome"], row["outcome_label"] = "rejected", "Respinta"
        elif question_quorum is None:
            row["outcome_label"] = "Quorum non configurato"
        elif not row["answered"]:
            row["outcome_label"] = "Nessuna risposta"
        elif row["kind"] in {Question.Kind.MOTION, Question.Kind.MOTION_REQUIRED}:
            favorable = dict(row["choices"]).get("Favorevole", 0)
            row["approval_minimum"] = row["answered"] // 2 + 1
            if favorable >= row["approval_minimum"]:
                row["outcome"], row["outcome_label"] = "approved", "Approvata"
            else:
                row["outcome"], row["outcome_label"] = "rejected", "Respinta"
        else:
            row["outcome"], row["outcome_label"] = "", ""
    return {**data, "quorum": quorum, "quorum_failed": quorum_failed}


def ballot_summary(questions, answers):
    return [
        (
            question,
            [
                choice.text
                for choice in question.choices.all()
                if choice.pk in answers.get(str(question.pk), [])
            ],
        )
        for question in questions
    ]


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


def assembly_outcome_lines(result):
    assembly, quorum = result["assembly"], result["quorum"]
    state = (
        "Quorum costitutivo raggiunto"
        if quorum["met"]
        else "Assemblea non validamente costituita"
    )
    return [
        f"Assemblea {assembly['type'].lower()} · {assembly['convocation']} · verifica {assembly['revision']}",
        f"Configurazione v{assembly['version']} · {assembly['members']} associati · {result['eligible']} aventi diritto",
        f"{quorum['counted']} partecipanti riconosciuti · {assembly['direct']} in proprio · {assembly['represented']} per sola rappresentanza",
        f"{assembly['absent']} aventi diritto non computati · {result['total']} schede elettroniche",
        f"{state} · {quorum['label']} · minimo {quorum['minimum']}",
    ]


def deliberation_outcome_lines(row, *, include_outcome=True):
    result = []
    if include_outcome and row.get("outcome_label"):
        result.append(row["outcome_label"])
    threshold = row.get("quorum")
    if threshold is not None:
        result.append(
            f"Quorum del quesito · {threshold['label']} · {threshold['counted']} risposte · minimo {threshold['minimum']}"
        )
    rule = row.get("deliberative")
    if rule is not None:
        result.append(
            f"{row['matter']} · {row['participants']} partecipanti aventi diritto · {row['no_choice']} senza scelta"
        )
        if rule["minimum"] is not None:
            result.extend(
                (
                    f"{rule['criterion']} · {rule['label']}",
                    f"Denominatore {rule['denominator']} · minimo {rule['minimum']} favorevoli",
                )
            )
    return result


def aggregate_csv(election, *, data=None):
    result = results_with_outcomes(election) if data is None else data
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
    if "assembly" in result:
        rows.append([])
        rows.extend([line] for line in assembly_outcome_lines(result))
    configured = [row for row in result["rows"] if has_question_rules(row)]
    if configured:
        rows.append(
            [
                "Quesito",
                "Quorum del quesito",
                "Minimo votanti",
                "Esito quorum",
                "Tipologia",
                "Partecipanti aventi diritto al quesito",
                "Favorevoli",
                "Contrari",
                "Astenuti",
                "Senza scelta",
                "Regola deliberativa",
                "Soglia deliberativa",
                "Denominatore",
                "Minimo favorevoli",
                "Esito",
            ]
        )
        for row in configured:
            rule = row.get("deliberative", {})
            threshold = row.get("quorum") or {}
            rows.append(
                [
                    row["question"],
                    threshold.get("label"),
                    threshold.get("minimum"),
                    {True: "Raggiunto", False: "Non raggiunto"}.get(
                        threshold.get("met")
                    ),
                    row.get("matter"),
                    row.get("participants"),
                    row.get("favorable"),
                    row.get("against"),
                    row.get("abstained"),
                    row.get("no_choice"),
                    rule.get("criterion"),
                    rule.get("label"),
                    rule.get("denominator"),
                    rule.get("minimum"),
                    row["outcome_label"],
                ]
            )
    return csv_response("risultati.csv", rows)


def has_question_rules(row):
    return "deliberative" in row or bool(row.get("constitutive_quorum"))


def participation_filename(election):
    return "aventi-diritto.csv" if election.is_secret else "partecipazione.csv"


def participation_csv(election):
    filename = participation_filename(election)
    if election.is_secret:
        return csv_response(
            filename,
            [
                ["ID socio", "Nome", "Cognome", "Sezione"],
                *election.voters.values_list(
                    "external_id", "first_name", "last_name", "section"
                ),
            ],
        )
    ballot_times = dict(
        election.participation_records().values_list("voter_id", "submitted_at")
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
    return csv_response(filename, rows)


def presence_records(election):
    return (
        VoterPresence.objects.filter(voter__election=election)
        .select_related("voter")
        .order_by("confirmed_at", "pk")
    )


def presences_csv(election):
    rows = [
        [
            "ID socio",
            "Nome",
            "Cognome",
            "Sezione",
            "Presenza registrata (Europe/Rome)",
            "Origine",
        ]
    ]
    for item in presence_records(election).iterator():
        voter = item.voter
        rows.append(
            [
                voter.external_id,
                voter.first_name,
                voter.last_name,
                voter.section,
                timezone.localtime(item.confirmed_at).isoformat(timespec="seconds"),
                item.get_source_display(),
            ]
        )
    return csv_response("presenze.csv", rows)


def eligible_roll(election):
    return election.voters.values_list("external_id", "first_name", "last_name")


def named_vote_details(election):
    questions = ballot_questions(election)
    ballots = (
        election.participation_records()
        .select_related("voter", "cast_by")
        .order_by("voter__last_name", "voter__first_name", "voter__external_id")
    )
    for ballot in ballots:
        yield (
            ballot.voter,
            ballot.cast_by,
            ballot_summary(questions, ballot.answers),
            not ballot.answers,
        )


def voting_appendix(election, styles, width, *, title=None, eligible_only=False):
    body = styles["BodyText"]
    if election.is_secret or eligible_only:
        title = title or "Elenco degli aventi diritto"
        rows = [["Matricola", "Nome", "Cognome"]]
        rows.extend(
            [Paragraph(escape(value), body) for value in voter]
            for voter in eligible_roll(election)
        )
        widths = [width * 0.22, width * 0.38, width * 0.40]
        empty = "Nessun avente diritto."
    else:
        title = title or "Votanti e scelte per quesito"
        rows = [["Elettore", "Scelte registrate"]]
        for voter, delegate, answers, blank in named_vote_details(election):
            name = f"<b>{escape(f'{voter.first_name} {voter.last_name}')}</b><br/>{escape(voter.external_id)}"
            if delegate:
                name += f"<br/>Delega esercitata da {escape(f'{delegate.first_name} {delegate.last_name}')} ({escape(delegate.external_id)})"
            lines = [
                Paragraph(
                    f"<b>{index}. {escape(question.text)}</b><br/>{escape(', '.join(choices) or 'Senza risposta')}",
                    body,
                )
                for index, (question, choices) in enumerate(answers, start=1)
            ]
            if blank:
                lines.insert(0, Paragraph("Scheda bianca", body))
            rows.append([Paragraph(name, body), lines])
        widths = [width * 0.30, width * 0.70]
        empty = "Nessuna scheda acquisita."
    heading = Paragraph(title, styles["Heading1"])
    if len(rows) == 1:
        return [PageBreak(), heading, Paragraph(empty, body)]
    table = LongTable(rows, colWidths=widths, repeatRows=1, splitInRow=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5eff3")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#f1f6f6")],
                ),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return [PageBreak(), heading, table]


def scrutiny_pdf_buffer(election, *, data=None):
    if data is None:
        data = results_with_outcomes(election)
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
        Paragraph(
            f"Chiusura {ended} (Europe/Rome) · Voto {election.get_mode_display().lower()}",
            body,
        ),
        Paragraph("Riepilogo non firmato a supporto della verbalizzazione.", body),
        Paragraph(
            f"Aventi diritto <b>{data['eligible']}</b> · Schede valide <b>{data['total']}</b>"
            f" · Schede bianche <b>{data['blank']}</b>",
            body,
        ),
    ]
    if "assembly" in data:
        story.append(Paragraph("Verifica assembleare", styles["Heading2"]))
        story.extend(
            Paragraph(escape(line), body) for line in assembly_outcome_lines(data)
        )
    for row in data["rows"]:
        story.append(Paragraph(escape(row["question"]), styles["Heading2"]))
        if has_question_rules(row):
            story.extend(
                Paragraph(escape(line), body)
                for line in deliberation_outcome_lines(row)
            )
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
    story.extend(voting_appendix(election, styles, 435))
    doc.build(story)
    buffer.seek(0)
    return buffer


def summary_pdf(election):
    return FileResponse(
        scrutiny_pdf_buffer(election),
        content_type="application/pdf",
        as_attachment=True,
        filename="scrutinio.pdf",
    )
