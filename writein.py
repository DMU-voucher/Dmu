"""The write-in pad, as a Word file.

A pad of vouchers with the value, the expiry and the code left as placeholders,
for the day this app or the computer it runs on is not available. Word rather
than PDF so the office can type into it: a fallback that can only be filled in
by hand is a worse fallback than one that can be filled in either way.

**This is a second drawing of the artwork, and that is the cost of the format.**
Everywhere else there is exactly one definition of what a voucher looks like,
in templates/_voucher.html and static/voucher.css, shared by the screen preview
and both PDF engines. Word reads neither, so the layout below is built again in
python-docx and the two have to be kept in step by hand.

What is *not* duplicated is the wording. Every string on the voucher comes out
of config.json the same way the artwork's does, so changing a venue or a line of
small print changes this file too without anybody editing it. The duplication is
the shape, not the content, which is the half that changes least.

python-docx is pure Python and small, so unlike Chromium it is no trouble on the
server.
"""

from __future__ import annotations

import io
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.enum.text import (WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING,
                            WD_TAB_ALIGNMENT)
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Mm, Pt, RGBColor

import vouchers as core

# Read off the stylesheet rather than picked again, so the two drawings of the
# artwork at least agree about colour. The names match the CSS variables.
DMU_RED = RGBColor(0x9D, 0x09, 0x32)
DMU_RED_DARK = RGBColor(0x6D, 0x06, 0x23)
FD_ORANGE = RGBColor(0xF0, 0x7F, 0x1A)
INK = RGBColor(0x00, 0x00, 0x00)
INK_SOFT = RGBColor(0x66, 0x66, 0x66)
RED_WASH = "FAF1F4"
RULE = "C9C9C9"
CUT = "B8B8B8"

BODY_FONT = "Segoe UI"
CODE_FONT = "Consolas"

# A voucher is a 105 x 99mm cell, six to an A4 page, which is the grid the
# scissors expect. Same numbers as the .sheet grid in voucher.css.
CELL_W = Mm(105)
# 94mm, not the artwork's 99, and the difference is not arbitrary.
#
# Word does not lay a row out in the height you ask for. Measured by converting
# the file with Word and reading the pitch between two vouchers off the PDF, it
# adds a consistent 4.2mm to whatever trHeight says, so a 94mm row occupies
# 98.2mm of page. That is the number that has to fit, not 94.
#
# Three of those is 294.6mm. A table also cannot be the last thing in a Word
# document or sit next to another table, so a paragraph always follows it and
# the page needs room for that too; it is set to a hairline below. The total
# comes in just under A4 with about 2mm to spare.
#
# Asking for 99mm instead put two vouchers on a page and made a pad of 100 come
# out 34 pages long. A cut-out voucher 1mm short of the printed ones is not
# something anybody can see. That pagination is.
CELL_H = Mm(94)
ROW_OVERHEAD_MM = 4.2
CELL_PAD = Mm(4)
# What is left to write in once the padding is taken off. The right-hand tab
# stop that pushes "Food & Drink Voucher" to the edge is set to this.
CONTENT_W = Mm(97)

# The placeholders. Deliberately not ruled lines: a blank rule left unfilled
# looks like a voucher somebody meant to write on, while an unfilled [code] is
# obviously an error and gets caught before it is handed over.
VALUE_MARK = "[value]"
DATE_MARK = "[date]"
CODE_MARK = "[code]"

# Where the code panel sits. On the printed artwork it is pinned to the foot of
# the voucher by a flex layout; Word has nothing that pins a paragraph to the
# bottom of a cell, so the gap above it is padded out instead.
#
# These two are measured, not guessed, by converting the file with Word and
# reading the panel off the PDF: with the minimum spacer, a three venue voucher
# runs to 77.0mm from the top of its cell, and every venue after that adds
# 4.2mm. Whatever is left over between that and the foot is the padding.
#
# Without it the panel floated in the middle of the voucher with a blank third
# underneath, which reads as a voucher somebody forgot to finish.
CONTENT_END_MM = 77.0
CONTENT_PER_VENUE_MM = 4.2

# Spare venue lines. One is drawn as an empty bullet at the end of the list, and
# one more is height held back at the foot with nothing to show for it.
#
# The pad is a Word file so that it can be edited, and adding a venue is one of
# the things people edit. The list has to be able to grow, and the cell it grows
# inside cannot: a row set to an exact height does not grow and does not
# complain, it just stops drawing, so anything pushed past the floor goes
# quietly missing until it reaches paper.
#
# Three attempts sit behind this one, and they were all the same argument about
# whether the spare line is drawn. Holding the room and drawing nothing was
# invisible, and room nobody can find is worth nothing. Drawing it as a bullet
# with an underlined rule beside it was findable, but the rule reads as a line
# somebody meant to write on, printed a hundred times. Saying it in words beside
# the download link, which is where the last version left it, only tells the
# person who downloads the pad; it tells nobody who is handed the file later.
#
# So the bullet is drawn and the rule is not: an empty bullet is a place to type
# rather than a blank to fill in, and it is on the artwork where the person
# editing it is looking. What went wrong last time it was drawn is fixed in
# _venue_line rather than argued about again: the gap after the dot belongs to
# the name's run, so a venue typed onto the blank line comes out in 8.5pt bold
# black like the ones above it instead of the bullet's own 6pt orange.
#
# The cost is the bullet itself, on all 100 vouchers, whether anybody uses it or
# not. That is the trade being made here and it is the user's call, not this
# file's.
#
# Both lines are counted in the spacer below, the drawn one because it takes a
# venue's height and the held one because the whole point is that it can. The
# spacer is worked out when the file is built and does not recompute when
# somebody types, so room held is room given up at the foot. Measured by
# converting with Word and reading the panel off the PDF, against the 90mm
# floor:
#
#   4 venues, blank line empty   panel ends 83.7mm,  6.3mm spare
#   5, one typed in                         84.9mm,  5.1mm
#   6, two typed in                         89.1mm,  0.9mm
#   7, three typed in                       93.2mm,  3.2mm PAST
#
# The first venue typed in costs only 1.2mm because the blank line is drawn no
# taller than its own 6pt dot until there are words on it; every one after that
# costs a full 4.2mm and walks the panel further down.
#
# A seventh belongs in config.json, which rebuilds the pad around it and puts it
# on the real vouchers too, which typing into Word does not.
SPARE_VENUE_LINES_DRAWN = 1
SPARE_VENUE_ROOM_LINES = 1


# --------------------------------------------------------------------------
# The XML that python-docx has no API for
# --------------------------------------------------------------------------

def _el(tag: str, **attrs) -> OxmlElement:
    node = OxmlElement(tag)
    for key, value in attrs.items():
        node.set(qn("w:" + key), str(value))
    return node


def _shade(element, fill: str) -> None:
    """Paint a paragraph or cell background."""
    props = element.get_or_add_pPr() if hasattr(element, "get_or_add_pPr") else element
    props.append(_el("w:shd", val="clear", color="auto", fill=fill))


def _borders(props, edges: dict, tag: str = "w:pBdr") -> None:
    """Draw some of the four edges. `edges` maps top/left/bottom/right to
    (style, eighths of a point, colour)."""
    holder = OxmlElement(tag)
    for side in ("top", "left", "bottom", "right"):
        if side not in edges:
            continue
        style, size, colour = edges[side]
        holder.append(_el("w:" + side, val=style, sz=size, space=0, color=colour))
    props.append(holder)


def _cell_margins(cell, pad: Mm) -> None:
    props = cell._tc.get_or_add_tcPr()
    margins = OxmlElement("w:tcMar")
    for side in ("top", "start", "bottom", "end"):
        margins.append(_el("w:" + side, w=int(pad.twips), type="dxa"))
    props.append(margins)


def _table_indent(table, pad: Mm) -> None:
    """Put the table's left edge on the left margin, not the cell's text.

    Left alone, Word lines the first cell's *contents* up with the margin, which
    hangs the table itself out to the left by one cell margin. On a page with no
    margin at all that puts the left-hand column of vouchers half a millimetre
    off the paper and leaves 7.5mm spare down the right: measured off the PDF,
    the whole grid sat 4.5mm to the left and the sheet printed visibly skewed.

    Setting the indent explicitly is what stops that. There is no python-docx
    API for it.
    """
    table._tbl.tblPr.append(_el("w:tblInd", w=int(pad.twips), type="dxa"))


def _cut_guides(table) -> None:
    """Dashed lines between the vouchers and nothing around the outside.

    The dashes are where the scissors go. The outside edge needs none: that is
    the edge of the paper, and a dashed line printed along it only invites
    somebody to trim the margin off.
    """
    props = table._tbl.tblPr
    holder = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right"):
        holder.append(_el("w:" + side, val="none", sz=0, space=0, color="auto"))
    for side in ("insideH", "insideV"):
        holder.append(_el("w:" + side, val="dashed", sz=4, space=0, color=CUT))
    props.append(holder)


# --------------------------------------------------------------------------
# Paragraph helpers
# --------------------------------------------------------------------------

def _para(cell, *, space_before=0, space_after=0, line=None, align=None):
    """A paragraph with the tight spacing the artwork uses. Word's defaults
    would put 8pt under every line, which is most of a voucher."""
    p = cell.add_paragraph()
    fmt = p.paragraph_format
    fmt.space_before = Pt(space_before)
    fmt.space_after = Pt(space_after)
    if line is not None:
        fmt.line_spacing = line
    if align is not None:
        fmt.alignment = align
    return p


def _run(p, text: str, *, size: float, bold=False, color=None, font=BODY_FONT):
    r = p.add_run(text)
    r.font.name = font
    r.font.size = Pt(size)
    r.bold = bold
    if color is not None:
        r.font.color.rgb = color
    return r


def _placeholder(p, text: str, *, size: float, font=BODY_FONT):
    """Something to type over. Left mid-grey so an unfilled one is visible from
    across a desk, and bold so it is easy to double-click and replace."""
    return _run(p, text, size=size, bold=True, color=INK_SOFT, font=font)


def _venue_line(cell, name: str):
    """One venue on its bullet, or an empty bullet to type one onto.

    The two spaces after the dot belong to the name's run rather than the
    bullet's, and that is the whole reason the blank line can be drawn at all.
    Typing at the end of a paragraph in Word takes the formatting of the
    character to the left of the cursor, so with the gap left in the bullet's
    own 6pt orange a venue typed onto the blank came out 6pt orange. In the
    name's 8.5pt bold black it comes out looking like the venues above it.

    Real lines are drawn through here too, so every name starts in the same
    place as the one that gets typed in.
    """
    line = _para(cell, space_after=0.5, line=1.0)
    line.paragraph_format.left_indent = Mm(1)
    _run(line, "●", size=6, color=FD_ORANGE)
    _run(line, "  " + name, size=8.5, bold=True, color=INK)
    return line


# --------------------------------------------------------------------------
# One voucher
# --------------------------------------------------------------------------

def _voucher(cell, config: dict, logo: Path | None) -> None:
    venues = [v for v in (config.get("venues") or []) if str(v).strip()]

    # ---- the lockup, over a hairline, as on the artwork
    head = _para(cell, space_after=1.5)
    if logo is not None:
        head.add_run().add_picture(str(logo), height=Mm(8))
    _borders(head.paragraph_format.element.get_or_add_pPr(),
             {"bottom": ("single", 4, RULE)})

    # ---- the value, and the title pushed to the right-hand edge
    value = _para(cell, space_before=2, space_after=1)
    value.paragraph_format.tab_stops.add_tab_stop(CONTENT_W, WD_TAB_ALIGNMENT.RIGHT)
    _run(value, "£", size=20, color=DMU_RED)
    _placeholder(value, VALUE_MARK, size=16)
    _run(value, "\t", size=8)
    _run(value, config.get("voucher_title") or "", size=8, bold=True, color=INK_SOFT)

    # ---- where it can be spent
    _run(_para(cell, space_after=0.5), config.get("venue_intro") or "",
         size=7.5, bold=True, color=INK_SOFT)
    for name in venues:
        _venue_line(cell, name)
    # The line the next venue gets typed onto, left empty. Not a [vendor]
    # placeholder: that would print the word "[vendor]" on all 100 vouchers as
    # somewhere they can be spent, unless every one of them is cleared first.
    for _ in range(SPARE_VENUE_LINES_DRAWN):
        _venue_line(cell, "")

    _run(_para(cell, space_before=1.5, space_after=1),
         config.get("venue_warning") or "", size=7, bold=True, color=DMU_RED)

    # ---- what the holder is told to do, in its shaded box
    action = _para(cell, space_before=0.5, space_after=2)
    action.paragraph_format.left_indent = Mm(1.5)
    _shade(action.paragraph_format.element, "F5F5F5")
    _borders(action.paragraph_format.element.get_or_add_pPr(),
             {"left": ("single", 18, "F07F1A")})
    _run(action, "  " + (config.get("holder_instruction") or ""), size=8)

    # ---- the expiry, over a hairline
    dates = _para(cell, space_before=1.5, space_after=1)
    _borders(dates.paragraph_format.element.get_or_add_pPr(),
             {"top": ("single", 4, RULE)})
    _run(dates, (config.get("valid_until_label") or "") + " ", size=7, color=INK_SOFT)
    _placeholder(dates, DATE_MARK, size=7)

    for line in (config.get("small_print") or []):
        _run(_para(cell, space_after=0, line=1.0), line, size=5.5, color=INK_SOFT)

    # ---- the code panel. Two paragraphs sharing one border and one fill, so it
    # reads as a single box without a nested table: a table inside a table is
    # noticeably harder to type in, and typing in it is the point of this file.
    # Everything the venue list has not already spent, between the end of the
    # small print and the bottom padding. Clamped at nothing to give away, which
    # is what a list longer than the artwork's own six-venue ceiling leaves.
    content_end = CONTENT_END_MM + CONTENT_PER_VENUE_MM * (
        len(venues) - 3 + SPARE_VENUE_LINES_DRAWN + SPARE_VENUE_ROOM_LINES)
    room = (CELL_H.mm - CELL_PAD.mm) - content_end
    label = _para(cell, space_before=max(3.0, room * 72 / 25.4), space_after=0,
                  align=WD_ALIGN_PARAGRAPH.CENTER)
    _shade(label.paragraph_format.element, RED_WASH)
    _borders(label.paragraph_format.element.get_or_add_pPr(), {
        "top": ("single", 6, "9D0932"),
        "left": ("single", 6, "9D0932"),
        "right": ("single", 6, "9D0932"),
    })
    _run(label, config.get("reference_label") or "", size=7, bold=True,
         color=DMU_RED)

    code = _para(cell, space_before=0, space_after=2,
                 align=WD_ALIGN_PARAGRAPH.CENTER)
    _shade(code.paragraph_format.element, RED_WASH)
    _borders(code.paragraph_format.element.get_or_add_pPr(), {
        "left": ("single", 6, "9D0932"),
        "right": ("single", 6, "9D0932"),
        "bottom": ("single", 6, "9D0932"),
    })
    _placeholder(code, CODE_MARK, size=18, font=CODE_FONT)


# --------------------------------------------------------------------------
# The pad
# --------------------------------------------------------------------------

def _logo_path() -> Path | None:
    for name in core.LOGO_FILES["dmu"]:
        path = core.ASSETS_DIR / name
        # Word will not place an SVG, and the artwork's own fallback order puts
        # the bitmaps first anyway.
        if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg"):
            return path
    return None


def build(config: dict, count: int) -> bytes:
    """`count` write-in vouchers, six to an A4 page, as .docx bytes."""
    count = max(1, min(int(count), core.BLANK_PAD_MAX))
    per_page = int(config.get("vouchers_per_page") or 6)
    logo = _logo_path()

    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    for side in ("top", "bottom", "left", "right"):
        setattr(section, f"{side}_margin", Mm(0))
    # Word puts a header and footer band inside the margins; at a zero margin
    # they would still push the first row down the page.
    section.header_distance = Mm(0)
    section.footer_distance = Mm(0)

    style = doc.styles["Normal"]
    style.font.name = BODY_FONT
    style.font.size = Pt(9)
    style.paragraph_format.space_after = Pt(0)

    made = 0
    while made < count:
        on_this_page = min(per_page, count - made)
        rows = -(-per_page // 2)  # always the full grid, so the cuts line up
        table = doc.add_table(rows=rows, cols=2)
        table.alignment = WD_TABLE_ALIGNMENT.LEFT
        table.autofit = False
        _table_indent(table, CELL_PAD)
        _cut_guides(table)

        for row in table.rows:
            row.height = CELL_H
            row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY

        for index in range(rows * 2):
            cell = table.cell(index // 2, index % 2)
            cell.width = CELL_W
            _cell_margins(cell, CELL_PAD)
            if index < on_this_page:
                # The paragraph every cell starts life with, which would
                # otherwise sit above the lockup and push the voucher down its
                # cell. Only dropped where a voucher replaces it: a <w:tc> with
                # no block content at all is invalid, and Word refuses the whole
                # file as corrupt rather than ignoring the empty cell. That only
                # shows up on a pad whose last page is not full, which is every
                # pad of 100.
                first = cell.paragraphs[0]._element
                first.getparent().remove(first)
                _voucher(cell, config, logo)

        made += on_this_page

        # The paragraph Word insists on after a table. Set to a hairline so it
        # costs the page almost nothing, and carrying the page break itself
        # rather than having a second paragraph for that.
        gap = doc.add_paragraph()
        gap.paragraph_format.space_before = Pt(0)
        gap.paragraph_format.space_after = Pt(0)
        gap.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        gap.paragraph_format.line_spacing = Pt(1)
        run = gap.add_run()
        run.font.size = Pt(1)
        if made < count:
            run.add_break(WD_BREAK.PAGE)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()
