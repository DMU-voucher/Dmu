"""DMU Food & Drink voucher generator.

Drop in the voucher approval export, get print-ready vouchers out. Runs
entirely on this machine: nothing is uploaded anywhere.

Start it with run.bat (Windows) or run.command (Mac).
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import secrets
import tempfile
import time
import traceback
import zipfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_file, url_for)

import vouchers as core

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # a CSV export is tiny

UPLOADS_DIR = core.DATA_DIR / "Uploads"
SESSIONS_DIR = core.DATA_DIR / "Sessions"
CSS_PATH = core.APP_DIR / "static" / "voucher.css"

# Served on a public address rather than started with run.bat. Changes what the
# pages say and how finished batches are collected: there is no folder to open
# on a server, and nothing lands in Dropbox afterwards.
HOSTED = os.environ.get("DMU_HOSTED", "").strip().lower() in ("1", "true", "yes")

# One password in front of the whole site. Empty is the office machine, where
# run.bat binds to 127.0.0.1 and the only way in is to be sitting at it. Hosted
# it is mandatory, and site_gate below refuses to serve anything without it.
SITE_USER = os.environ.get("DMU_SITE_USER", "dmu")
SITE_PASSWORD = os.environ.get("DMU_SITE_PASSWORD", "")


# --------------------------------------------------------------------------
# The password in front of everything
# --------------------------------------------------------------------------

@app.before_request
def site_gate():
    """The browser password prompt, if there is one.

    Off when DMU_SITE_PASSWORD is empty. That is the office machine, where the
    app is not reachable from anywhere else in the first place.

    Hosted with no password set, every route is refused rather than served. An
    open copy of this app prints DMU vouchers for anyone who finds the address,
    and /ledger hands over every number ever issued, so a misconfigured copy has
    to fail loudly instead of quietly working.
    """
    if not SITE_PASSWORD:
        if HOSTED:
            return Response(
                "This copy is not set up yet: DMU_SITE_PASSWORD is empty, so "
                "nothing is served. Set it and reload the web app.",
                503, {"Content-Type": "text/plain; charset=utf-8"})
        return None

    auth = request.authorization
    supplied_user = (auth.username or "") if auth else ""
    supplied_pass = (auth.password or "") if auth else ""

    # compare_digest on both, and both always evaluated, so a wrong password
    # takes the same time as a right one and neither can be timed against the
    # other.
    user_ok = secrets.compare_digest(supplied_user, SITE_USER)
    pass_ok = secrets.compare_digest(supplied_pass, SITE_PASSWORD)
    if user_ok and pass_ok:
        return None
    return Response("Sign in to use the voucher generator.", 401,
                    {"WWW-Authenticate": 'Basic realm="DMU vouchers"'})

# Food and Drink has no mark of its own, so there is only one logo to find. The
# list lives in vouchers.py because the thumbnail fingerprint hashes the same
# files, and the two must not be able to drift apart.
LOGO_FILES = core.LOGO_FILES


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


def thumbnail_uri() -> str | None:
    """The pre-made picture of a voucher, for the vendor sheet.

    A data URI like the logos, so the rendered HTML stays self-contained and
    neither PDF engine has anything to fetch. None if it has not been made yet,
    and the template falls back to the live artwork.
    """
    return (data_uri(core.THUMBNAIL_PATH)
            if core.THUMBNAIL_PATH.is_file() else None)


def render_sheet(vouchers_: list[core.Voucher], config: dict, title: str) -> str:
    per_page = int(config.get("vouchers_per_page") or 6)
    return render_template(
        "sheet.html",
        pages=core.chunk(vouchers_, per_page),
        per_page=per_page,
        title=title,
        **render_context(config),
    )


def render_vendor_sheet(config: dict) -> str:
    """The handout. Always shows the fixed specimen rather than a voucher out of
    the batch, so the sheet never quotes a number that was really issued."""
    return render_template("vendor.html",
                           specimen=core.specimen_voucher(len(config.get("venues") or [])),
                           thumbnail=thumbnail_uri(),
                           **render_context(config))


def render_thumbnail_page(config: dict) -> str:
    """The specimen voucher alone, for make_sample_thumbnail.py to photograph."""
    return render_template("thumbnail.html",
                           specimen=core.specimen_voucher(len(config.get("venues") or [])),
                           **render_context(config))


# --------------------------------------------------------------------------
# Uploads waiting to be turned into vouchers
# --------------------------------------------------------------------------
#
# On disk, not in memory. Locally that only buys an upload surviving a restart.
# Hosted it is the difference between working and not: a server runs more than
# one worker process and recycles them when it likes, so an upload held in one
# process's memory is invisible to the request that arrives three minutes later
# with the dates filled in. That fails as "that upload has expired",
# intermittently, halfway through a batch.

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _session_path(token: str) -> Path | None:
    """None for anything that is not a token we issued, rather than letting a
    made-up one choose a filename."""
    if not token or not TOKEN_PATTERN.fullmatch(token):
        return None
    return SESSIONS_DIR / f"{token}.json"


def save_session(token: str, session: dict) -> None:
    path = _session_path(token)
    if path is None:
        raise ValueError("bad session token")
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    stored = dict(session)
    stored["requests"] = [asdict(r) for r in session["requests"]]
    path.write_text(json.dumps(stored), encoding="utf-8")
    _sweep_sessions()


def load_session(token: str) -> dict | None:
    path = _session_path(token)
    if path is None or not path.is_file():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["requests"] = [core.VoucherRequest(**r)
                              for r in stored["requests"]]
    except (OSError, ValueError, TypeError):
        return None
    return stored


def _sweep_sessions(older_than_hours: int = 24) -> None:
    """An upload is working state, not a record: the CSV itself is kept in
    Uploads and the vouchers in the ledger. Anything still here a day later was
    abandoned."""
    cutoff = datetime.now().timestamp() - older_than_hours * 3600
    try:
        for path in SESSIONS_DIR.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError:
        pass


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
        "qr": qr_quality(config),
        "thumbnail_state": core.thumbnail_state(config),
        "hosted": HOSTED,
        # The same data URIs the vouchers use, so the page wears the real
        # artwork the moment somebody drops the files into assets/ and nothing
        # has to be pointed at a second copy of them.
        "logos": logo_uris(),
        # What a run costs on this machine, for the progress bar. Measured from
        # the last run rather than assumed, because the office machine and the
        # server draw with different engines at different speeds.
        "pace": core.read_pace(),
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

    token = secrets.token_urlsafe(12)
    save_session(token, {
        "requests": parsed.requests,
        "skipped": parsed.skipped,
        "source_csv": str(saved_path) if saved_path else "",
        "filename": upload_file.filename,
    })

    return render_template("index.html", parsed=_parsed_context(token),
                           submitted={}, **base_context(config))


def _parsed_context(token: str) -> dict | None:
    """Rebuild the review table from a stored upload.

    Used both after an upload and after a rejected generate, so that being told
    to tick a box does not mean choosing the file all over again.
    """
    session = load_session(token)
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
    """The print sheet as it will come out, carrying the specimen code.

    Nothing is written to the ledger, and every voucher shows 00-000 rather than
    its real code, because no request has ID 0. A preview does get printed by
    accident occasionally and it must be impossible for that sheet to carry a
    code somebody could hand over.
    """
    session = load_session(token)
    if not session or index >= len(session["requests"]):
        abort(404)

    config = core.load_config()
    req = session["requests"][index]
    # The dates ride on the request now, straight from the export, so a preview
    # shows the same ones the print will. Only the venues are still a choice, so
    # they are the only thing worth carrying in the query string.
    venues = core.resolve_venues(config, request.args.getlist("venues"),
                                 request.args.get("extra_venue", ""))
    vs = core.build_vouchers(req, venues, specimen=True)
    return render_sheet(vs, config, f"Preview - {req.event_name}")


@app.get("/preview-vendor")
def preview_vendor():
    config = core.load_config()
    return render_vendor_sheet(config)


@app.get("/vendor-instructions.pdf")
def vendor_instructions():
    """The vendor handout, on its own.

    It used to be written into every batch folder, which meant the requestor's
    zip carried a sheet meant for vendors and the office had four copies of the
    same page. Taking it out of the folder needed this first: on the hosted copy
    the batch zip was the only way to get it as a PDF at all, since the preview
    above is HTML and the loose copy beside the records is never served.

    Rendered fresh rather than served from LOOSE_VENDOR_PDF, so it always
    matches what config.json says today. It is one page, and it is the same
    sheet whichever batch you came from.
    """
    config = core.load_config()
    try:
        with core.PdfWriter() as writer:
            buf = io.BytesIO()
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "Vendor instructions.pdf"
                writer.write(render_vendor_sheet(config), path)
                buf.write(path.read_bytes())
        buf.seek(0)
    except Exception:
        traceback.print_exc()
        return _index_with_error(
            "The vendor sheet could not be drawn. The details are in the window "
            "running the app. The vouchers themselves are unaffected: this is "
            "the handout, and it can also be printed from the preview link at "
            "the bottom of this page.")
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name="Vendor instructions.pdf")


@app.post("/generate")
def generate():
    token = request.form.get("token", "")
    session = load_session(token)
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

    issued_by = (request.form.get("issued_by") or "").strip()
    override = request.form.get("override_duplicates") == "on"

    # No dates to read or check here any more. Both come off the row in the
    # export, and a row without a usable expiry never reaches this point:
    # parse_csv puts it in skipped rather than offering it to be ticked.
    venues = core.resolve_venues(config, request.form.getlist("venues"),
                                 request.form.get("extra_venue", ""))

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
            f"{prev['codes'][-1]}). A second set would carry those same codes "
            f"again, because a code is the ID and the ticket number rather than "
            f"something drawn fresh, so the two sets could not be told apart. "
            f"Untick it, or tick 'issue these again anyway' further down if you "
            f"really do need a second set.",
            token=token,
        )

    issued_at = datetime.now()
    batch = core._next_batch_number(ledger)
    results = []
    started = time.perf_counter()

    try:
        with core.PdfWriter() as writer:
            for i in selected:
                req = session["requests"][i]
                vs = core.build_vouchers(req, venues)
                # One folder per request, with the ID in its name, because this
                # folder is what gets sent to whoever asked for the vouchers.
                out_dir = core.unique_output_dir(req.event_name, issued_at, req.dmu_id)
                out_dir.mkdir(parents=True, exist_ok=True)

                # Named for the batch, because these files leave the folder:
                # two batches downloaded as two zips used to unpack into two
                # files called Print sheet.pdf.
                sheet_pdf = out_dir / core.batch_file_name(
                    "Print sheet", req.event_name, req.dmu_id, ".pdf")
                writer.write(render_sheet(vs, config, req.event_name), sheet_pdf)

                core.write_batch_summary(
                    out_dir / core.batch_file_name(
                        "Batch summary", req.event_name, req.dmu_id, ".csv"),
                    req, vs, batch, issued_by, issued_at, venues)

                # No vendor sheet and no copy of the export in here. The vendor
                # sheet is the same handout for every batch in the run and goes
                # to vendors rather than to the requestor, so it is downloaded
                # on its own from the done page. The export copy was the whole
                # uploaded file, so sending one requestor their folder showed
                # them every other request in it; the file itself is still kept
                # in Uploads for the audit trail.

                core.append_ledger([{
                    "dmu_code": v.dmu_code,
                    "batch": f"{batch:03d}",
                    "event_name": req.event_name,
                    "cost_centre": req.cost_centre,
                    "value_pence": req.value_pence,
                    "valid_until": req.expiry_date,
                    "event_date": req.event_date,
                    "budget_approver": req.budget_approver,
                    "lead_contact": req.lead_contact,
                    "issued_at": issued_at.isoformat(timespec="seconds"),
                    "issued_by": issued_by,
                    "output_folder": str(out_dir),
                } for v in vs])

                results.append({
                    "event_name": req.event_name,
                    "dmu_id": req.dmu_id,
                    "count": req.count,
                    "value_display": req.value_display,
                    "total_display": req.total_display,
                    "batch": f"{batch:03d}",
                    "first_code": vs[0].dmu_code,
                    "last_code": vs[-1].dmu_code,
                    # Per event now, because the export carries a date per row.
                    "valid_until": core.format_uk_date(req.expiry_date),
                    "event_date": core.format_uk_date(req.event_date),
                    "folder": str(out_dir),
                    "pages": -(-len(vs) // int(config.get("vouchers_per_page") or 6)),
                })
                batch += 1

            # Refresh the loose copy that lives with the records. Its own
            # try/except, because the ledger has already been written by this
            # point: a failure here must not be reported as "nothing was
            # written anywhere", which is what the handler below says.
            try:
                writer.write(render_vendor_sheet(config), core.LOOSE_VENDOR_PDF)
            except Exception:
                traceback.print_exc()
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

    # What this run actually cost, so the next one's progress bar is measured
    # rather than guessed. Only ever recorded from a run that finished.
    core.record_pace(sum(r["count"] for r in results),
                     time.perf_counter() - started)

    return render_template(
        "done.html",
        cfg=config,
        hosted=HOSTED,
        results=results,
        venues=venues,
        logos=logo_uris(),
        issued_by=issued_by,
        qr_ready=core.qr_url_configured(config),
        qr_url=core.qr_url(config),
    )


@app.post("/open-folder")
def open_folder():
    # Nothing to open a folder on when the app is served over the web.
    if HOSTED:
        abort(404)
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


@app.post("/download")
def download_batch():
    """One finished batch as a single zip.

    What replaces "Open the folder" when the app is not running on the machine
    you are sitting at. Built in memory: a batch is a few PDFs and two CSVs, and
    a temporary file on a server is only something else to tidy up.
    """
    folder = Path(request.form.get("folder", ""))
    try:
        folder = folder.resolve()
        relative = folder.relative_to(core.OUTPUT_DIR.resolve())
    except (OSError, ValueError):
        abort(400)
    # relative_to succeeds for Output itself as well, which would hand over
    # every batch ever made in a single file.
    if not relative.parts:
        abort(400)
    if not folder.is_dir():
        abort(404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder).as_posix())
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{folder.name}.zip")


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
        "sample_thumbnail": core.thumbnail_state(config),
    })


if __name__ == "__main__":
    print()
    print("  DMU Food & Drink voucher generator")

    print("  Open this in your browser:  http://127.0.0.1:5057")
    print("  Leave this window open while you use it.")
    print()
    app.run(host="127.0.0.1", port=5057, debug=False)
