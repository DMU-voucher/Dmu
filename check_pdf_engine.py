"""Render a sample voucher sheet and report what the PDF came out like.

Run this anywhere the app is installed, and it says which engine it used, how
many pages it made, what size they are and anything the engine could not draw.

    python3.10 check_pdf_engine.py

The point is the server. Chromium is 427 MB and will not fit on a free
PythonAnywhere account, so the server renders with WeasyPrint, and this is how
you find out whether WeasyPrint reproduced the layout before a real voucher is
printed wrong. Force one engine with DMU_PDF_ENGINE=weasyprint.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import vouchers as core
import refs

OUT = Path(__file__).resolve().parent / "engine-check"


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

    # Throwaway references, so nothing reaches the ledger.
    sample = [refs.make_reference(p) for p in range(100001, 100013)]
    req = core.VoucherRequest(
        row_number=2, event_name="Engine check", count=len(sample),
        value_pence=600, total_value_pence=600 * len(sample),
        cost_centre="CHECK", budget_approver="", budget_approver_email="",
        lead_contact="", lead_contact_email="", status="Approved",
        # An ID and both dates, so the check exercises the DMU code and the
        # event date line. Those are the newest bits of layout, and layout is
        # exactly where the two PDF engines disagree.
        dmu_id="99", expiry_date="2026-09-24", event_date="2026-09-10",
    )
    vs = core.build_vouchers(req, sample, core.resolve_venues(config, None))

    # Importing the Flask app only for its template rendering.
    import app as generator
    with generator.app.test_request_context():
        sheet_html = generator.render_sheet(vs, config, "Engine check")
        singles_html = generator.render_singles(vs, config, "Engine check")
        vendor_html = generator.render_vendor_sheet(config)

    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [("sheet", sheet_html), ("singles", singles_html), ("vendor", vendor_html)]

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

    print()
    print(f"  PDFs written to {OUT}")
    print("  Open sheet.pdf and check: six vouchers to a page, dashed cut lines,")
    print("  the voucher number large and centred in its red panel, and nothing")
    print("  overlapping or clipped. Then open vendor.pdf and check it is one")
    print("  page, with the QR square solid black and the fallback address")
    print("  wrapped inside its border.")
    print()
    return 0


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
