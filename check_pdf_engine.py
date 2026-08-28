"""Render a sample voucher sheet, then check the PDF the engine actually made.

Run this anywhere the app is installed, with the pip that matches the Python the
web app uses:

    python3.10 check_pdf_engine.py

It reports which engine it used, the page count and size, anything the engine
could not draw, and then **measures the rendered vouchers** for overlapping
text, text escaping its voucher, and text printed over the voucher-code box. It
exits non-zero if it finds any of those, so it can be treated as a test.

**Run it on the server.** Chromium is 427 MB and will not fit on a free
PythonAnywhere account, so the server renders with WeasyPrint, and the two
engines do not agree. In August 2026 a layout that was perfect under Chromium
shipped with the voucher code printed on top of the event name and the small
print sliced in half by the code box, because nothing had ever rendered it with
WeasyPrint. Chromium passing here proves nothing about the server. Force one
engine with DMU_PDF_ENGINE=weasyprint.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import vouchers as core

OUT = Path(__file__).resolve().parent / "engine-check"

# The print sheet's geometry, from voucher.css: an A4 page holding a 2 x 3 grid
# of 105 x 99mm cells, one voucher each.
CELL_MM = (105.0, 99.0)
GRID = (2, 3)
MM = 72 / 25.4  # millimetres to PDF points

# Text boxes carry slack above and below the glyphs, so tightly stacked lines
# overlap slightly without anything being wrong: the DMU lockup sets its two
# lines at a line-height of 1.05 and they do touch. Judge a collision by how
# much of the smaller box is buried rather than by whether the boxes meet at
# all. The failure this exists to catch buried one string in another almost
# entirely, so the threshold has plenty of room under it.
OVERLAP_SHARE = 0.25

# Boxes are still pulled in a hair before the outside-the-voucher test, so a
# glyph resting exactly on the cut line is not called an escape.
SLACK = 0.5

# The voucher's own top and bottom padding, from .voucher in voucher.css. Text
# must stay inside it. The cell test above is too generous to catch the fault
# this exists for: the voucher is a fixed cell whose foot cannot grow, so
# anything that makes the artwork taller pushes the bottom line down through
# the padding and into the border, and it is still inside the 99mm cell while
# it does it. Vertical only. Glyph side bearings make the same test sideways
# report ordinary type as a fault, and width is not the constrained axis here.
VOUCHER_PAD_MM = 3.0


def main() -> int:
    print()
    print("  Engines on this machine")
    for name, state in core.pdf_engines_available().items():
        print(f"    {name:12} {state}")

    config = core.load_config()

    state = core.thumbnail_state(config)
    print()
    print(f"  Vendor sheet's voucher picture: {state}")
    if state != "ready":
        print("    Run make_sample_thumbnail.py. Until then the vendor sheet")
        print("    falls back to CSS-scaled artwork, which WeasyPrint prints")
        print("    with an empty voucher-number panel.")

    url_ok = core.qr_url_configured(config)
    print()
    print(f"  QR link configured: {url_ok}")
    if url_ok:
        q = core.qr_quality(core.qr_url(config))
        print(f"  QR: {q['module_mm']} mm per square, needs {core.QR_MIN_MODULE_MM}, ok={q['ok']}")

    venues = core.resolve_venues(config, None)

    # Nothing here reaches the ledger: these are built by hand, not generated.
    req = core.VoucherRequest(
        row_number=2, event_name="Engine check", count=12,
        value_pence=600, total_value_pence=600 * 12,
        cost_centre="CHECK", budget_approver="", budget_approver_email="",
        lead_contact="", lead_contact_email="", status="Approved",
        # An ID and both dates, so the check exercises the voucher code and the
        # event date line. Layout is exactly where the two engines disagree.
        dmu_id="99", expiry_date="2026-09-24", event_date="2026-09-10",
    )
    vs = core.build_vouchers(req, venues)

    # A second sheet of deliberately awkward vouchers: the longest codes the
    # scheme can produce, and an event name past the truncation point. A batch
    # of 12-01s proves nothing about what happens when DMU's IDs grow.
    def awkward(code: str, name: str) -> core.Voucher:
        return core.Voucher(dmu_code=code, value_display="£6.00", event_name=name,
                            valid_until="30 September 2026",
                            event_date="10 September 2026", venues=venues)

    stress = [
        awkward("50000-8000", "Five figure ID, four figure ticket"),
        awkward("999999-999999", "Six and six, the last size that stays large"),
        awkward("1234567-1234567", "Fifteen characters, one size down"),
        awkward("123456789-123456789", "Nineteen characters, smallest size"),
        awkward("12-01", "An event name long enough that it has to be truncated "
                         "rather than run off the edge of the voucher"),
        awkward("12-02", "Short"),
    ]

    # Importing the Flask app only for its template rendering.
    import app as generator
    with generator.app.test_request_context():
        sheet_html = generator.render_sheet(vs, config, "Engine check")
        stress_html = generator.render_sheet(stress, config, "Awkward codes")
        vendor_html = generator.render_vendor_sheet(config)

    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [("sheet", sheet_html), ("stress", stress_html), ("vendor", vendor_html)]

    with core.PdfWriter() as writer:
        print()
        print(f"  Rendering with: {writer.engine}")
        for name, html in jobs:
            path = OUT / f"{name}.pdf"
            writer.write(html, path)
            describe(name, path)
        if writer.warnings:
            print()
            print(f"  {len(writer.warnings)} thing(s) the engine could not draw:")
            for w in dict.fromkeys(writer.warnings):
                print(f"    - {w}")
        else:
            print()
            print("  The engine reported nothing it could not draw.")

    problems: list[str] = []
    for name in ("sheet", "stress"):
        problems += check_layout(OUT / f"{name}.pdf", config, name)

    print()
    if problems:
        print(f"  LAYOUT: {len(problems)} problem(s) in the rendered PDFs")
        for p in problems:
            print(f"    - {p}")
        print()
        print("  These are measured from the PDF, not guessed. Open the file")
        print("  named and look at the voucher named before changing anything.")
    else:
        print("  Layout: no overlapping text, nothing outside its voucher, and")
        print("  nothing printed over the code box.")

    print()
    print(f"  PDFs written to {OUT}")
    print("  Open sheet.pdf and check: six vouchers to a page, dashed cut lines,")
    print("  the voucher code large and centred in its red panel, and nothing")
    print("  overlapping or clipped. Then open vendor.pdf and check it is one")
    print("  page, with the QR square solid black and the fallback address")
    print("  wrapped inside its border.")
    print()
    return 1 if problems else 0


# --------------------------------------------------------------------------
# Measuring the rendered PDF
# --------------------------------------------------------------------------

def check_layout(path: Path, config: dict, label: str) -> list[str]:
    """Measure the vouchers on a rendered sheet and report anything wrong.

    Three faults, all of which have actually shipped:

      - two pieces of text printed on top of each other,
      - text running outside the voucher it belongs to,
      - text printed over the voucher-code box, which is what a voucher whose
        content is too tall looks like once the sheet clips it.

    Measured from the PDF rather than the HTML on purpose. The HTML is identical
    on both engines; it is the drawing that differs.
    """
    try:
        import fitz
    except ImportError:
        return [f"{label}: cannot check layout, pymupdf is not installed"]

    code_label = str(config.get("reference_label") or "").strip()
    problems: list[str] = []

    with fitz.open(path) as doc:
        for page_no, page in enumerate(doc, start=1):
            spans = _text_spans(page, fitz)
            boxes = [fitz.Rect(d["rect"]) for d in page.get_drawings()]

            for cell_no, cell in enumerate(_cells(page, fitz), start=1):
                where = f"{label}.pdf page {page_no}, voucher {cell_no}"
                here = [(r, t) for r, t in spans if r.intersects(cell)]
                if not here:
                    continue  # an empty cell, which is normal on a part sheet

                for rect, text in here:
                    if not cell.contains(_shrink(rect, fitz)):
                        problems.append(f"{where}: {_short(text)} runs outside the voucher")

                border = _voucher_border(cell, boxes)
                if border is not None:
                    top = border.y0 + VOUCHER_PAD_MM * MM
                    bottom = border.y1 - VOUCHER_PAD_MM * MM
                    for rect, text in here:
                        r = _shrink(rect, fitz)
                        if r.y0 < top or r.y1 > bottom:
                            problems.append(
                                f"{where}: {_short(text)} runs into the voucher's "
                                f"margin, so the artwork is taller than the cell")

                for i, (rect_a, text_a) in enumerate(here):
                    for rect_b, text_b in here[i + 1:]:
                        if _buried(rect_a, rect_b) >= OVERLAP_SHARE:
                            problems.append(
                                f"{where}: {_short(text_a)} is printed on top of "
                                f"{_short(text_b)}")

                code_box = _code_box(cell, boxes, here, code_label)
                if code_box is None:
                    problems.append(f"{where}: no {code_label!r} box found")
                    continue
                for rect, text in here:
                    if code_box.contains(_shrink(rect, fitz)):
                        continue  # the label and the code itself live in there
                    if _shrink(rect, fitz).intersects(code_box):
                        problems.append(
                            f"{where}: {_short(text)} is printed over the "
                            f"{code_label} box")

    return problems


def _text_spans(page, fitz) -> list:
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                if text:
                    out.append((fitz.Rect(span["bbox"]), text))
    return out


def _cells(page, fitz) -> list:
    """The six voucher rectangles on a print sheet page.

    Text escaping its own cell is a real fault: the cells are what gets cut up,
    so anything crossing a boundary is printed on the wrong voucher.
    """
    wide, tall = CELL_MM[0] * MM, CELL_MM[1] * MM
    cells = []
    for row in range(GRID[1]):
        for col in range(GRID[0]):
            rect = fitz.Rect(col * wide, row * tall, (col + 1) * wide, (row + 1) * tall)
            if rect.intersects(page.rect):
                cells.append(rect)
    return cells


def _voucher_border(cell, boxes):
    """The drawn rectangle of the voucher itself: the biggest box in the cell."""
    inside = [b for b in boxes if cell.contains(b)]
    return max(inside, key=lambda b: abs(b.get_area())) if inside else None


def _buried(a, b) -> float:
    """How much of the smaller box the overlap covers, 0 to 1."""
    overlap = a & b
    if overlap.is_empty:
        return 0.0
    smaller = min(abs(a.get_area()), abs(b.get_area()))
    return abs(overlap.get_area()) / smaller if smaller else 0.0


def _code_box(cell, boxes, spans, code_label):
    """The red panel, found as the smallest drawn box holding the code label."""
    label_rect = next((r for r, t in spans if t == code_label and cell.contains(r)), None)
    if label_rect is None:
        return None
    holding = [b for b in boxes if b.contains(label_rect) and cell.contains(b)]
    return min(holding, key=lambda b: abs(b.get_area())) if holding else None


def _shrink(rect, fitz):
    """Pull a box in on all sides, without letting it turn inside out.

    A span narrower than twice the slack, a single thin character, would
    otherwise come back inverted, and an inverted rectangle intersects things it
    has no business intersecting.
    """
    pad_x = min(SLACK, max(rect.width - 0.1, 0) / 2)
    pad_y = min(SLACK, max(rect.height - 0.1, 0) / 2)
    return fitz.Rect(rect.x0 + pad_x, rect.y0 + pad_y,
                     rect.x1 - pad_x, rect.y1 - pad_y)


def _short(text: str) -> str:
    text = " ".join(text.split())
    return repr(text if len(text) <= 34 else text[:31] + "...")


def describe(name: str, path: Path) -> None:
    size_kb = path.stat().st_size / 1024
    try:
        import fitz
        doc = fitz.open(path)
        pages = doc.page_count
        w, h = doc[0].rect.width, doc[0].rect.height
        # PDF points to mm.
        wmm, hmm = w * 25.4 / 72, h * 25.4 / 72
        doc.close()
        print(f"    {name:8} {pages} page(s)  {wmm:.1f} x {hmm:.1f} mm  {size_kb:.0f} KB"
              f"   {'A4' if abs(wmm - 210) < 1 and abs(hmm - 297) < 1 else 'NOT A4'}")
    except ImportError:
        print(f"    {name:8} {size_kb:.0f} KB  (install pymupdf for page details)")


if __name__ == "__main__":
    sys.exit(main())
