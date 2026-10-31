"""Render parsed handout elements to DOCX with a theme.

Everything uses features Google Docs keeps when it opens a DOCX on a phone:
paragraph styles, inline images, simple tables with cell shading. No text boxes,
floating shapes, footnotes or fields.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from workspace_paths import ROOT

THEMES = ROOT / "system/config/handout-themes"
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def load_theme(name=None):
    name = name or "default"
    path = THEMES / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"找不到講義主題：{name}")
    base = json.loads((THEMES / "default.json").read_text(encoding="utf-8"))
    theme = json.loads(path.read_text(encoding="utf-8"))
    for key, value in theme.items():  # a series theme only lists what it changes
        base[key] = {**base[key], **value} if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return base


def _rgb(hex_value):
    return RGBColor.from_string(hex_value.upper())


def _fonts(rpr_owner, latin, east):
    rpr = rpr_owner.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        fonts.attrib.pop(qn(attr), None)
    fonts.set(qn("w:ascii"), latin)
    fonts.set(qn("w:hAnsi"), latin)
    fonts.set(qn("w:eastAsia"), east)
    fonts.set(qn("w:cs"), latin)


def _style(document, name, theme, size, color, bold=False, before=0, after=6, spacing=None):
    style = document.styles[name]
    _fonts(style.element, theme["fonts"]["latin"], theme["fonts"]["east_asian"])
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.italic = False
    style.font.color.rgb = _rgb(color)
    fmt = style.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    if spacing:
        fmt.line_spacing = spacing
    return style


def _shade(cell, fill):
    props = cell._tc.get_or_add_tcPr()
    shade = OxmlElement("w:shd")
    shade.set(qn("w:val"), "clear")
    shade.set(qn("w:color"), "auto")
    shade.set(qn("w:fill"), fill)
    props.append(shade)


def _borders(table, color, sides=("top", "left", "bottom", "right", "insideH", "insideV"), size=6, only=None):
    props = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        edge = OxmlElement(f"w:{side}")
        if side in sides:
            width = (only or {}).get(side, size)
            edge.set(qn("w:val"), "single")
            edge.set(qn("w:sz"), str(width))
            edge.set(qn("w:color"), color)
        else:
            edge.set(qn("w:val"), "nil")
        borders.append(edge)
    props.append(borders)


def _inline(paragraph, text, size=None, color=None, bold=False):
    pieces = _BOLD.split(text)
    for index, piece in enumerate(pieces):
        if not piece:
            continue
        run = paragraph.add_run(piece)
        run.bold = bold or index % 2 == 1
        if size:
            run.font.size = Pt(size)
        if color:
            run.font.color.rgb = _rgb(color)
    return paragraph


def render(meta, chapters, assets, theme, output):
    """assets maps id(element) -> {'path': png, 'width_px': css width} for images and diagrams."""
    reference = theme.get("reference_docx")
    document = Document(str(ROOT / reference)) if reference else Document()
    if reference:  # keep the designer's styles, drop sample content
        body = document.element.body
        for child in list(body):
            if child.tag != qn("w:sectPr"):
                body.remove(child)
    colors, fonts, sizes = theme["colors"], theme["fonts"], theme["sizes"]

    section = document.sections[0]
    section.page_width, section.page_height = Cm(theme["page"]["width_cm"]), Cm(theme["page"]["height_cm"])
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Cm(theme["page"]["margin_cm"]))
    usable_cm = theme["page"]["width_cm"] - 2 * theme["page"]["margin_cm"]

    if not reference:
        _style(document, "Normal", theme, fonts["body_pt"], colors["text"], after=8, spacing=fonts["line_spacing"])
        _style(document, "Title", theme, sizes["title_pt"], colors["heading"], bold=True, after=4)
        _style(document, "Subtitle", theme, sizes["subtitle_pt"], colors["muted"], after=18)
        _style(document, "Heading 1", theme, sizes["chapter_pt"], colors["heading"], bold=True, before=18, after=8)
        _style(document, "Heading 2", theme, sizes["subheading_pt"], colors["accent"], bold=True, before=12, after=6)
        _style(document, "Caption", theme, fonts["small_pt"], colors["muted"], before=2, after=10)
        _style(document, "List Bullet", theme, fonts["body_pt"], colors["text"], after=4, spacing=fonts["line_spacing"])
        document.styles["Title"].element.get_or_add_pPr()
        for border in document.styles["Title"].element.pPr.findall(qn("w:pBdr")):
            document.styles["Title"].element.pPr.remove(border)

    document.core_properties.title = meta["title"]
    document.core_properties.author = ""
    document.core_properties.comments = ""
    document.add_paragraph(meta["title"], style="Title")
    if meta.get("subtitle"):
        document.add_paragraph(str(meta["subtitle"]), style="Subtitle")

    for chapter in chapters:
        document.add_paragraph(chapter["title"], style="Heading 1")
        for element in chapter["elements"]:
            kind = element["type"]
            if kind == "paragraph":
                _inline(document.add_paragraph(style="Normal"), element["text"])
            elif kind == "subheading":
                document.add_paragraph(element["text"], style="Heading 2")
            elif kind == "bullets":
                for item in element["items"]:
                    _inline(document.add_paragraph(style="List Bullet"), item)
            elif kind == "numbered":
                for number, item in enumerate(element["items"], 1):
                    paragraph = document.add_paragraph(style="Normal")
                    paragraph.paragraph_format.left_indent = Cm(0.9)
                    paragraph.paragraph_format.first_line_indent = Cm(-0.6)
                    paragraph.paragraph_format.space_after = Pt(4)
                    _inline(paragraph, f"{number}. {item}")
            elif kind == "table":
                _table(document, element, theme)
            elif kind == "callout":
                _callout(document, element, theme)
            elif kind in ("image", "mermaid"):
                asset = assets[id(element)]
                width_cm = min(usable_cm, theme["image_max_width_cm"])
                if kind == "mermaid":
                    width_cm = min(width_cm, asset["width_px"] / 96 * 2.54 * 1.05)
                else:
                    width_cm = min(width_cm, theme["frame_width_cm"])
                paragraph = document.add_paragraph(style="Normal")
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.paragraph_format.space_after = Pt(2)
                paragraph.add_run().add_picture(str(asset["path"]), width=Cm(width_cm))
                if element.get("caption"):
                    caption = document.add_paragraph(element["caption"], style="Caption")
                    caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output))


def _table(document, element, theme):
    colors, fonts = theme["colors"], theme["fonts"]
    rows = [element["header"], *element["rows"]]
    table = document.add_table(rows=len(rows), cols=len(element["header"]))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(table, colors["table_border"])
    short = all(len(re.sub(r"\*\*", "", c)) <= 4 for row in rows for c in row)
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.2
            if short:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _inline(paragraph, value, size=fonts["small_pt"], bold=(r == 0))
            if r == 0:
                _shade(cell, colors["table_header_fill"])
    spacer = document.add_paragraph(style="Normal")
    spacer.paragraph_format.space_after = Pt(4)


def _callout(document, element, theme):
    colors = theme["colors"]
    table = document.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _borders(table, colors["callout_border"], sides=("left",), only={"left": 24})
    cell = table.cell(0, 0)
    _shade(cell, colors["callout_fill"])
    first = True
    for line in element["lines"]:
        paragraph = cell.paragraphs[0] if first else cell.add_paragraph()
        first = False
        paragraph.paragraph_format.space_after = Pt(2)
        _inline(paragraph, line)
    spacer = document.add_paragraph(style="Normal")
    spacer.paragraph_format.space_after = Pt(4)
