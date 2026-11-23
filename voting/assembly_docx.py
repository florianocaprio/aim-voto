import re
from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Mm, Pt, RGBColor

from . import reports

XML_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def docx_text(value):
    return XML_CONTROLS.sub("", str(value))


def report_table(document, headers, rows, widths):
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = False
    for column, width in zip(table.columns, widths, strict=True):
        column.width = width
    header = table.rows[0]
    header._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    for cell, label, width in zip(header.cells, headers, widths, strict=True):
        cell.width = width
        shading = OxmlElement("w:shd")
        shading.set(qn("w:fill"), "15373A")
        cell._tc.get_or_add_tcPr().append(shading)
        run = cell.paragraphs[0].add_run(label)
        run.bold = True
        run.font.color.rgb = RGBColor.from_string("FFFFFF")
    for row in rows:
        for cell, value, width in zip(table.add_row().cells, row, widths, strict=True):
            cell.width = width
            cell.text = docx_text(value)
    return table


def render(election, content, hashes):
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.left_margin = section.right_margin = Pt(48)
    section.top_margin = section.bottom_margin = Pt(48)
    section.footer_distance = Pt(20)
    width = section.page_width - section.left_margin - section.right_margin
    document.core_properties.title = "Verbale assembleare AIM VOTO"
    document.core_properties.author = "AIM VOTO"
    document.core_properties.comments = ""
    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(10)
    normal.paragraph_format.line_spacing = Pt(12)
    normal.paragraph_format.space_after = Pt(4)
    for name, size in (("Title", 20), ("Heading 1", 16), ("Heading 2", 13)):
        style = document.styles[name]
        style.font.name = "Arial"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string("000000")
        style.paragraph_format.line_spacing = 1.1
        style.paragraph_format.space_before = Pt(10)
        style.paragraph_format.space_after = Pt(5)
        style.paragraph_format.keep_with_next = True
    title_properties = document.styles["Title"].element.get_or_add_pPr()
    title_border = title_properties.find(qn("w:pBdr"))
    if title_border is not None:
        title_properties.remove(title_border)

    def paragraph(text, style=None):
        return document.add_paragraph(docx_text(text), style=style)

    def table(headers, rows, proportions):
        return report_table(
            document,
            headers,
            rows,
            [Emu(int(width * proportion)) for proportion in proportions],
        )

    footer_style = document.styles["Footer"]
    footer_style.paragraph_format.tab_stops.clear_all()
    footer_style.font.size = Pt(8)
    footer = section.footer.paragraphs[0]
    footer.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
    footer.add_run(
        f"AIM VOTO  |  Consultazione {election.pk}  |  Verbale assembleare\tPagina "
    )
    page = OxmlElement("w:fldSimple")
    page.set(qn("w:instr"), "PAGE")
    footer._p.append(page)

    paragraph("VERBALE ASSEMBLEARE", "Title").alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph(election.title, "Heading 1")
    if election.minutes_agenda.strip():
        paragraph("Ordine del giorno", "Heading 2")
        paragraph(election.minutes_agenda)
    if election.minutes_discussion.strip():
        paragraph("Discussione", "Heading 2")
        paragraph(election.minutes_discussion).paragraph_format.space_after = Pt(36)
    paragraph(content["intro"])
    references = content["references"]
    table(references[0], references[1:], (0.40, 0.60))
    paragraph("Partecipazione e rappresentanze", "Heading 2")
    participation = content["participation"]
    table(participation[0], participation[1:], (0.84, 0.16))
    paragraph(content["participation_note"])
    quorum_title, quorum_lines = content["quorum"]
    if quorum_title:
        paragraph(quorum_title, "Heading 2")
        for line in quorum_lines:
            paragraph(line)
    paragraph(content["legal_note"])

    paragraph("Esito delle votazioni", "Heading 1").paragraph_format.page_break_before = True
    for question in content["questions"]:
        paragraph(question["title"], "Heading 2")
        if question["outcome"]:
            status = paragraph("")
            status.paragraph_format.keep_with_next = True
            run = status.add_run(docx_text(question["outcome"]))
            run.bold = True
            run.font.size = Pt(12)
        paragraph(question["summary"])
        for line in question["notes"]:
            paragraph(line)
        table(("Opzione", "Preferenze"), question["choices"], (0.82, 0.18))

    paragraph("Impronte SHA-256", "Heading 2")
    paragraph("Le impronte si riferiscono ai prospetti di supporto calcolati all’esportazione.")
    digests = table(("File", "Impronta digitale"), hashes.items(), (0.29, 0.71))
    for row in digests.rows[1:]:
        run = row.cells[1].paragraphs[0].runs[0]
        run.font.name = "Courier New"
        run.font.size = Pt(8)

    paragraph("Sottoscrizione del verbale", "Heading 2")
    if content["signing_details"]:
        paragraph(content["signing_details"]).paragraph_format.keep_with_next = True
    signatures = document.add_table(rows=1, cols=2)
    signatures.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    for cell, role, name in zip(
        signatures.rows[0].cells,
        ("Firma del presidente", "Firma del segretario"),
        (election.minutes_president, election.minutes_secretary),
        strict=True,
    ):
        cell.paragraphs[0].add_run(role)
        cell.add_paragraph().add_run(docx_text(name)).bold = True
        line = cell.add_paragraph("________________________________")
        line.paragraph_format.space_before = Pt(20)

    paragraph("Appendice A - Aventi diritto al voto", "Heading 1").paragraph_format.page_break_before = True
    eligible = list(reports.eligible_roll(election))
    if eligible:
        table(("Matricola", "Nome", "Cognome"), eligible, (0.22, 0.38, 0.40))
    else:
        paragraph("Nessun avente diritto.")
    if not election.is_secret:
        paragraph("Appendice B - Votanti e scelte per quesito", "Heading 1").paragraph_format.page_break_before = True
        rows = []
        for voter, delegate, answers, blank in reports.named_vote_details(election):
            voter_text = f"{voter.first_name} {voter.last_name}\n{voter.external_id}"
            if delegate:
                voter_text += f"\nDelega esercitata da {delegate.first_name} {delegate.last_name} ({delegate.external_id})"
            choices_text = ["Scheda bianca"] if blank else []
            choices_text.extend(
                f"{index}. {question.text}\n{', '.join(choices) or 'Senza risposta'}"
                for index, (question, choices) in enumerate(answers, start=1)
            )
            rows.append((voter_text, "\n".join(choices_text)))
        if rows:
            table(("Elettore", "Scelte registrate"), rows, (0.30, 0.70))
        else:
            paragraph("Nessuna scheda acquisita.")

    output = BytesIO()
    document.save(output)
    return output.getvalue()
