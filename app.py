"""DMU Food & Drink voucher generator.

Drop in the voucher approval export, get print-ready vouchers out. Runs
entirely on this machine: nothing is uploaded anywhere.

Start it with run.bat (Windows) or run.command (Mac).
"""

from __future__ import annotations

import base64
import mimetypes
import secrets
import traceback
from datetime import datetime, date, timedelta
from pathlib import Path

from flask import (Flask, abort, jsonify, render_template, request,
                   send_file, url_for)

import vouchers as core

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # a CSV export is tiny

UPLOADS_DIR = core.APP_DIR / "Uploads"
CSS_PATH = core.APP_DIR / "static" / "voucher.css"

# Parsed uploads waiting to be turned into vouchers. In memory only: closing the
# app forgets them, which is what we want for a tool that gets run once a week.
SESSIONS: dict[str, dict] = {}

LOGO_FILES = {
    "food_and_drink": ["food-and-drink-logo.png", "food-and-drink-logo.jpg",
                       "food-and-drink-logo.svg"],
    "dmu": ["dmu-logo.png", "dmu-logo.jpg", "dmu-logo.svg"],
}


# --------------------------------------------------------------------------
# Shared render helpers
# --------------------------------------------------------------------------

def read_css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def logo_uris() -> dict[str, str | None]:
    """Logos are embedded as data URIs so the HTML is self-contained.

    If a logo has not been supplied yet the template falls back to a CSS
    lockup, so the tool still works before the artwork arrives.
    """
    out: dict[str, str | None] = {}
    for key, candidates in LOGO_FILES.items():
        out[key] = None
        for name in candidates:
            path = core.ASSETS_DIR / name
            if path.is_file():
                out[key] = data_uri(path)
                break
    return out


def render_context(config: dict) -> dict:
    """The same QR code goes on every voucher, so it is rendered once here and
    handed to every template. What distinguishes one voucher from the next is
    the number printed under it, not the code itself."""
    qr = (core.qr_svg(core.qr_url(config))
          if core.qr_url_configured(config) else None)
    return {"cfg": config, "qr": qr, "logos": logo_uris(), "css": read_css()}


def render_sheet(vouchers_: list[core.Voucher], config: dict, title: str) -> str:
    per_page = int(config.get("vouchers_per_page") or 6)
    return render_template(
        "sheet.html",
        pages=core.chunk(vouchers_, per_page),
        per_page=per_page,
        title=title,
        **render_context(config),
    )


def render_singles(vouchers_: list[core.Voucher], config: dict, title: str) -> str:
    return render_template("single.html", vouchers=vouchers_, title=title,
                           **render_context(config))


def render_vendor_sheet(sample: core.Voucher, config: dict) -> str:
    return render_template("vendor.html", sample=sample, **render_context(config))


def sample_voucher(config: dict) -> core.Voucher:
    return core.Voucher(
        code=core.sample_references(1)[0],
        value_display="£6.00",
        event_name="Example event",
        valid_until=core.format_uk_date((date.today() + timedelta(days=30)).isoformat()),
        event_date="",
    )


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def base_context(config: dict) -> dict:
    """Everything the index page needs regardless of what is being shown."""
    ledger = core.read_ledger()
    return {
        "cfg": config,
        "qr_ready": core.qr_url_configured(config),
        "ledger_count": len(ledger),
        "last_issued": _last_issued(ledger),
        "default_valid_until": (date.today() + timedelta(days=30)).isoformat(),
        "qr": qr_quality(config),
    }


@app.get("/")
def index():
    config = core.load_config()
    return render_template("index.html", parsed=None, submitted={},
                           **base_context(config))


def qr_quality(config: dict) -> dict | None:
    """None when the redemption link is not set yet, so the template can tell
    the difference between 'not configured' and 'configured but too dense'."""
    if not core.qr_url_configured(config):
        return None
    return core.qr_quality(core.qr_url(config))


def _last_issued(ledger: list[dict]) -> str:
    if not ledger:
        return ""
    stamps = [r.get("issued_at", "") for r in ledger if r.get("issued_at")]
    if not stamps:
        return ""
    try:
        latest = max(datetime.fromisoformat(s) for s in stamps)
        return latest.strftime("%d %B %Y")
    except ValueError:
        return ""


@app.post("/upload")
def upload():
    config = core.load_config()
    upload_file = request.files.get("csv")

    if not upload_file or not upload_file.filename:
        return _index_with_error("Choose the voucher approval CSV first.")

    raw = upload_file.read()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return _index_with_error("Could not read that file. Export it from the "
                                 "form again as CSV and try once more.")

    parsed = core.parse_csv(text)

    # Keep a copy of exactly what was dropped in, for the audit trail.
    saved_path = None
    try:
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        saved_path = UPLOADS_DIR / f"{stamp} {Path(upload_file.filename).name}"
        saved_path.write_bytes(raw)
    except OSError:
        saved_path = None

    if parsed.errors:
        return _index_with_error(parsed.errors[0], skipped=parsed.skipped)

    ledger = core.read_ledger()
    token = secrets.token_urlsafe(12)
    SESSIONS[token] = {
        "requests": parsed.requests,
        "skipped": parsed.skipped,
        "source_csv": str(saved_path) if saved_path else "",
        "filename": upload_file.filename,
    }

    return render_template("index.html", parsed=_parsed_context(token),
                           submitted={}, **base_context(config))


def _parsed_context(token: str) -> dict | None:
    """Rebuild the review table from a stored upload.

    Used both after an upload and after a rejected generate, so that being told
    to tick a box does not mean choosing the file all over again.
    """
    session = SESSIONS.get(token)
    if not session:
        return None
    ledger = core.read_ledger()
    return {
        "token": token,
        "filename": session["filename"],
        "rows": [
            {"index": i, "request": req, "previous": core.previous_issue(req, ledger)}
            for i, req in enumerate(session["requests"])
        ],
        "skipped": session["skipped"],
    }


def _index_with_error(message: str, skipped: list[dict] | None = None,
                      token: str | None = None):
    config = core.load_config()
    return render_template(
        "index.html",
        parsed=_parsed_context(token) if token else None,
        submitted=request.form if request.method == "POST" else {},
        error=message,
        error_skipped=skipped or [],
        **base_context(config),
    ), 400


@app.get("/preview/<token>/<int:index>")
def preview(token: str, index: int):
    """The print sheet as it will come out, using specimen numbers.

    Nothing is written to the ledger. The numbers shown start 000, which no
    real voucher ever does, so a
    preview that gets printed by accident cannot be passed off as a voucher.
    """
    session = SESSIONS.get(token)
    if not session or index >= len(session["requests"]):
        abort(404)

    config = core.load_config()
    req = session["requests"][index]
    valid_until = request.args.get("valid_until", "")
    event_date = request.args.get("event_date", "")
    vs = core.build_vouchers(req, core.sample_references(req.count),
                             valid_until, event_date)
    return render_sheet(vs, config, f"Preview - {req.event_name}")


@app.get("/preview-vendor")
def preview_vendor():
    config = core.load_config()
    return render_vendor_sheet(sample_voucher(config), config)


@app.post("/generate")
def generate():
    token = request.form.get("token", "")
    session = SESSIONS.get(token)
    if not session:
        return _index_with_error("That upload has expired. Choose the CSV again.")

    config = core.load_config()
    selected = []
    for raw in request.form.getlist("selected"):
        try:
            i = int(raw)
        except ValueError:
            continue
        if 0 <= i < len(session["requests"]):
            selected.append(i)
    if not selected:
        return _index_with_error("Tick at least one event to make vouchers for.",
                                 token=token)

    valid_until = (request.form.get("valid_until") or "").strip()
    event_date = (request.form.get("event_date") or "").strip()
    issued_by = (request.form.get("issued_by") or "").strip()
    make_singles = request.form.get("make_singles") == "on"
    override = request.form.get("override_duplicates") == "on"

    if not valid_until:
        return _index_with_error("Enter the date the vouchers are valid until.",
                                 token=token)
    try:
        date.fromisoformat(valid_until)
    except ValueError:
        return _index_with_error("The valid until date is not a real date.",
                                 token=token)
    if event_date:
        try:
            date.fromisoformat(event_date)
        except ValueError:
            return _index_with_error("The event date is not a real date.",
                                     token=token)

    ledger = core.read_ledger()
    blocked = []
    for i in selected:
        req = session["requests"][i]
        prev = core.previous_issue(req, ledger)
        if prev and not override:
            blocked.append((req, prev))
    if blocked:
        req, prev = blocked[0]
        return _index_with_error(
            f"'{req.event_name}' looks like it has already been issued on "
            f"{prev['issued_at']} as batch {prev['batch']} "
            f"({prev['count']} vouchers, codes {prev['codes'][0]} to "
            f"{prev['codes'][-1]}). Untick it, or tick 'issue these again anyway' "
            f"further down if you really do need a second set.",
            token=token,
        )

    issued_at = datetime.now()
    batch = core._next_batch_number(ledger)
    results = []

    try:
        with core.PdfWriter() as writer:
            for i in selected:
                req = session["requests"][i]
                references = core.draw_references(req.count)
                vs = core.build_vouchers(req, references, valid_until, event_date)
                out_dir = core.unique_output_dir(req.event_name, issued_at)
                out_dir.mkdir(parents=True, exist_ok=True)

                sheet_pdf = out_dir / "Print sheet.pdf"
                writer.write(render_sheet(vs, config, req.event_name), sheet_pdf)

                singles_written = 0
                if make_singles:
                    singles_written = _write_single_pdfs(writer, vs, config, out_dir)

                writer.write(render_vendor_sheet(vs[0], config),
                             out_dir / "Vendor instructions.pdf")

                core.write_batch_summary(out_dir / "Batch summary.csv", req, vs,
                                         batch, valid_until, event_date,
                                         issued_by, issued_at)
                src = session.get("source_csv")
                core.copy_source_csv(Path(src) if src else None, out_dir)

                core.append_ledger([{
                    "voucher_code": v.code,
                    "batch": f"{batch:03d}",
                    "event_name": req.event_name,
                    "cost_centre": req.cost_centre,
                    "value_pence": req.value_pence,
                    "valid_until": valid_until,
                    "event_date": event_date,
                    "budget_approver": req.budget_approver,
                    "lead_contact": req.lead_contact,
                    "issued_at": issued_at.isoformat(timespec="seconds"),
                    "issued_by": issued_by,
                    "output_folder": str(out_dir),
                } for v in vs])

                results.append({
                    "event_name": req.event_name,
                    "count": req.count,
                    "value_display": req.value_display,
                    "total_display": req.total_display,
                    "batch": f"{batch:03d}",
                    "first_code": vs[0].code,
                    "last_code": vs[-1].code,
                    "folder": str(out_dir),
                    "pages": -(-len(vs) // int(config.get("vouchers_per_page") or 6)),
                    "singles": singles_written,
                })
                batch += 1

            # Refresh the loose copy that lives with the app.
            writer.write(render_vendor_sheet(sample_voucher(config), config),
                         core.APP_DIR / "Vendor instructions.pdf")
    except Exception:
        traceback.print_exc()
        return _index_with_error(
            "Something went wrong while making the PDFs. Nothing was written "
            "anywhere, so there is nothing to undo. "
            "The details are in the black command window behind this page. If "
            "it mentions 'playwright' or 'chromium', close the app and run "
            "run.bat again.",
            token=token,
        )

    return render_template(
        "done.html",
        cfg=config,
        results=results,
        valid_until=core.format_uk_date(valid_until),
        event_date=core.format_uk_date(event_date),
        issued_by=issued_by,
        qr_ready=core.qr_url_configured(config),
        qr_url=core.qr_url(config),
    )


def _write_single_pdfs(writer: core.PdfWriter, vs: list[core.Voucher],
                       config: dict, out_dir: Path) -> int:
    """One PDF per voucher, for emailing.

    Rendered as a single multi-page PDF then split with PyMuPDF, which is far
    quicker than driving the browser once per voucher.
    """
    import fitz

    singles_dir = out_dir / "Individual"
    singles_dir.mkdir(parents=True, exist_ok=True)

    combined = out_dir / "All vouchers (individual pages).pdf"
    writer.write(render_singles(vs, config, "Vouchers"), combined)

    written = 0
    with fitz.open(combined) as doc:
        if doc.page_count != len(vs):
            # Should not happen, but never guess which page is which voucher.
            raise RuntimeError(
                f"Expected {len(vs)} single-voucher pages, got {doc.page_count}"
            )
        for page_index, v in enumerate(vs):
            with fitz.open() as single:
                single.insert_pdf(doc, from_page=page_index, to_page=page_index)
                single.save(str(singles_dir / f"{v.code}.pdf"))
            written += 1
    return written


@app.post("/open-folder")
def open_folder():
    folder = Path(request.form.get("folder", ""))
    try:
        folder = folder.resolve()
        folder.relative_to(core.OUTPUT_DIR.resolve())
    except (OSError, ValueError):
        abort(400)
    if not folder.is_dir():
        abort(404)
    core.open_folder(folder)
    return ("", 204)


@app.get("/ledger")
def ledger_csv():
    if not core.LEDGER_PATH.exists():
        abort(404)
    return send_file(core.LEDGER_PATH, as_attachment=False, mimetype="text/plain")


@app.get("/health")
def health():
    config = core.load_config()
    return jsonify({
        "qr_url_configured": core.qr_url_configured(config),
        "qr_url": core.qr_url(config),
        "qr": qr_quality(config),
        "vouchers_issued": len(core.read_ledger()),
        "logos": {k: bool(v) for k, v in logo_uris().items()},
    })


if __name__ == "__main__":
    print()
    print("  DMU Food & Drink voucher generator")

    print("  Open this in your browser:  http://127.0.0.1:5057")
    print("  Leave this window open while you use it.")
    print()
    app.run(host="127.0.0.1", port=5057, debug=False)
