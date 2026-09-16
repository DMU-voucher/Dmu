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
import shutil
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
#
# There is no username to get wrong. A browser's sign-in box asks for one
# because that is what HTTP basic authentication is, but it is not a secret and
# it never was: only the password is checked. Two names were in play here at
# once in August 2026, the app's and PythonAnywhere's own, and the prompt gives
# no clue which is which, so signing in became a guessing game that locked the
# site's owner out of it.
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
    so a misconfigured copy has to fail loudly instead of quietly working.
    """
    if not SITE_PASSWORD:
        if HOSTED:
            return Response(
                "This copy is not set up yet: DMU_SITE_PASSWORD is empty, so "
                "nothing is served. Set it and reload the web app.",
                503, {"Content-Type": "text/plain; charset=utf-8"})
        return None

    auth = request.authorization
    supplied = (auth.password or "") if auth else ""

    # compare_digest rather than ==, so a wrong password takes the same time as
    # a right one and cannot be found a character at a time.
    if secrets.compare_digest(supplied, SITE_PASSWORD):
        return None
    return Response("Sign in to use the voucher generator. The username is not "
                    "checked: put anything in it, and the site password in the "
                    "password box.", 401,
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


def render_blank_pad(vouchers_: list[core.Voucher], config: dict) -> str:
    """The write-in pad: a cover page of instructions, then sheets of vouchers.

    The cover carries the instructions rather than leaving them to the README,
    because the pad is used on the day the app is not working and the paper is
    all anybody will have.
    """
    per_page = int(config.get("vouchers_per_page") or 6)
    return render_template(
        "blank_pad.html",
        pages=core.chunk(vouchers_, per_page),
        per_page=per_page,
        count=len(vouchers_),
        title="Blank DMU Food & Drink vouchers",
        **render_context(config),
    )


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
    Uploads and the vouchers in their own folders. Anything still here a day
    later was abandoned."""
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
    return {
        "cfg": config,
        "qr_ready": core.qr_url_configured(config),
        "qr": qr_quality(config),
        "thumbnail_state": core.thumbnail_state(config),
        "hosted": HOSTED,
        # The same data URIs the vouchers use, so the page wears the real
        # artwork the moment somebody drops the files into assets/ and nothing
        # has to be pointed at a second copy of them.
        "logos": logo_uris(),
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
        stamp = core.now_uk().strftime("%Y-%m-%d %H%M%S")
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
    return {
        "token": token,
        "filename": session["filename"],
        "rows": [
            {"index": i, "request": req}
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

    Nothing is written anywhere, and every voucher shows 00-000 rather than
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


def _pad_count() -> int:
    """How many blank vouchers were asked for, out of the query string.

    Anything unreadable comes back as the default rather than as an error. The
    pad is a fallback, and a fallback that refuses to print because somebody
    mistyped a number in a URL is not one.
    """
    try:
        return int(request.args.get("count") or core.BLANK_PAD_DEFAULT)
    except (TypeError, ValueError):
        return core.BLANK_PAD_DEFAULT


@app.get("/preview/blank-pad")
def preview_blank_pad():
    config = core.load_config()
    return render_blank_pad(
        core.blank_vouchers(_pad_count(), len(config.get("venues") or [])), config)


@app.get("/blank-vouchers.pdf")
def blank_vouchers_pdf():
    """The pad of write-in vouchers, for the day this app is not available.

    There is an obvious circularity here: the thing that draws the fallback is
    the thing the fallback stands in for. That is not a flaw to be engineered
    out, it is the instruction on the cover page, which says to print the pad
    now and keep the paper. A downloaded PDF that is never printed is worth
    exactly as much as no pad at all.

    Nothing is recorded and no numbers are issued: a blank voucher has no code
    to reserve. So this can be fetched as often as anybody likes, and two people
    fetching it get the same paper.
    """
    config = core.load_config()
    vs = core.blank_vouchers(_pad_count(), len(config.get("venues") or []))
    try:
        with core.PdfWriter() as writer:
            buf = io.BytesIO()
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "Blank vouchers.pdf"
                writer.write(render_blank_pad(vs, config), path)
                buf.write(path.read_bytes())
        buf.seek(0)
    except Exception:
        traceback.print_exc()
        return _index_with_error(
            "The blank vouchers could not be drawn. The details are in the "
            "window running the app. Nothing else is affected: this is the "
            "write-in pad, and it can also be printed from the preview link "
            "at the bottom of this page.")
    return send_file(buf, mimetype="application/pdf", as_attachment=True,
                     download_name="Blank vouchers to write in (%d).pdf" % len(vs))


# --------------------------------------------------------------------------
# Making the vouchers, a few pages at a time
# --------------------------------------------------------------------------
#
# A run used to be one request: click the button, and the browser held a single
# POST open until every voucher in the export had been drawn. That worked on the
# office machine and could not work hosted. PythonAnywhere puts a load balancer
# in front of the app which hangs up on any request still going after five
# minutes and answers 504 itself, and the export of 8 September was five
# requests totalling 1,090 vouchers, which is 182 A4 pages. Every row arrives
# ticked, so that is what one click asked for. Chromium on the office PC draws
# those 182 pages in about fifteen seconds; the server has no Chromium, draws
# with WeasyPrint, and shares a CPU with everyone else on the free tier. That
# is a different order of magnitude, and it ran out of time. What came back was
# not the app's own "something went wrong" page but the host's, because the run
# never reached the app's error handling at all.
#
# So the browser now asks for the run a slice at a time. Each request draws a
# few pages, says how far through it is and returns; no single one of them is
# anywhere near the limit, however big the export. The slices of a print sheet
# are joined back into one PDF when the event is done, so what lands in the
# folder is exactly what landed there before.
#
# The slices are sized from what this machine has just been measured doing
# rather than from a figure in a file, because the same code runs on two
# machines with two engines at very different speeds. The first slice of a run
# is deliberately small: it is the measurement the rest are sized from.

# What one request should aim to cost. Twenty times under the limit, which is
# the headroom that matters: a free PythonAnywhere account that has spent its
# daily CPU allowance is not stopped, it is slowed down, and a slice that
# normally takes fifteen seconds has to survive being several times that.
STEP_TARGET_SECONDS = 15.0

# The most the first slice of a run may be, in pages. Every later slice is sized
# from what this run has just been measured doing, but the first one has only
# the figure saved from the last run to go on, and that was measured by the
# other engine if the data folder has ever been copied between machines. So it
# is capped low enough that even a badly wrong figure costs seconds.
FIRST_STEP_MAX_PAGES = 4

# The most any one slice may be, however fast the machine looks. It bounds two
# things at once. A rate measured on an idle machine can flatter a busy one, and
# this is the ceiling on how wrong that can go. And a slice is held in memory as
# one string of HTML before it is drawn: DMU's logo is a data URI repeated once
# per voucher, so a page of six costs about 100 KB and forty pages would be over
# four megabytes handed to WeasyPrint on a free account's worker.
MAX_STEP_PAGES = 16

JOBS_DIR = core.DATA_DIR / "Jobs"


def _job_path(job_id: str) -> Path | None:
    if not job_id or not TOKEN_PATTERN.fullmatch(job_id):
        return None
    return JOBS_DIR / f"{job_id}.json"


def _replace(temp: Path, path: Path) -> None:
    """os.replace, allowing for what Windows does with it.

    A rename over an existing file is atomic on Linux, which is the server and
    the case this is here for. On Windows it is refused outright whenever
    anything else has the destination open, and on the office machine that
    means Dropbox indexing the very folder the app runs from: a run died with
    "Access is denied" partway through the first slice.

    So it is retried briefly and then done the plain way rather than failing a
    run over it. The rename is a safeguard against a torn file, not the job
    itself, and a machine that will not do it is no worse off than before.
    """
    for _ in range(10):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    try:
        path.write_bytes(temp.read_bytes())
    finally:
        temp.unlink(missing_ok=True)


def save_job(job: dict) -> None:
    """Written whole or not at all.

    Written beside itself and renamed over the top, rather than into the file
    directly. A rename is atomic, so a reader never catches this half done.
    Straight into the file, an interrupted or overlapping write leaves
    truncated JSON, and the only symptom of that is the run reporting itself
    expired partway through with no way back to where it had got to.
    """
    path = _job_path(job["job"])
    if path is None:
        raise ValueError("bad job id")
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(job), encoding="utf-8")
    _replace(temp, path)


def load_job(job_id: str) -> dict | None:
    path = _job_path(job_id)
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sweep_jobs(older_than_hours: int = 24) -> None:
    """A job is the state of a run in progress, not a record of one. What the
    run produced is in Output and stays there.

    The part-drawn pages go with it. They carry real voucher codes, so leaving
    them lying about is the same mistake as leaving an upload lying about, and
    a run abandoned by closing the tab is the ordinary way they are left.
    """
    cutoff = datetime.now().timestamp() - older_than_hours * 3600
    try:
        live = set()
        for path in JOBS_DIR.glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
            else:
                live.add(path.stem)
        for path in JOBS_DIR.iterdir():
            # By name against the jobs still here, not by age: a folder of
            # pages whose job has already gone is finished with whatever its
            # date says, and going by age alone would leave it another day.
            if path.is_dir():
                if path.name not in live:
                    shutil.rmtree(path, ignore_errors=True)
            # What an interrupted atomic write leaves: the half-written copy,
            # rather than a half-written job. Harmless, and still worth not
            # accumulating one per interrupted run.
            elif path.suffix == ".tmp" and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def _plan_run(config: dict) -> tuple[dict | None, str]:
    """Read the form and write down what the run is going to do.

    Every check the old single-request handler made, made once here, before a
    single voucher is drawn. Returns the job, or the message to put on the page.
    """
    _sweep_jobs()
    token = request.form.get("token", "")
    session = load_session(token)
    if not session:
        return None, "That upload has expired. Choose the CSV again."

    selected = []
    for raw in request.form.getlist("selected"):
        try:
            i = int(raw)
        except ValueError:
            continue
        if 0 <= i < len(session["requests"]):
            selected.append(i)
    if not selected:
        return None, "Tick at least one event to make vouchers for."

    # No dates to read or check here any more. Both come off the row in the
    # export, and a row without a usable expiry never reaches this point:
    # parse_csv puts it in skipped rather than offering it to be ticked.
    venues = core.resolve_venues(config, request.form.getlist("venues"),
                                 request.form.get("extra_venue", ""))

    # Nothing is checked against a previous run and nothing refuses to print.
    # This is a printing tool: a code is the export's ID and a ticket number, so
    # printing the same request twice produces the same vouchers, and telling
    # somebody they cannot reprint a lost sheet was getting in the way more than
    # it was protecting anything.
    job = {
        "job": secrets.token_urlsafe(12),
        "token": token,
        "issued_by": (request.form.get("issued_by") or "").strip(),
        # UK time, not the host's. See core.now_uk(): this stamp goes on the
        # summary and names the batch folder, and the server runs on UTC. Taken
        # once for the whole run, so a run that spans midnight does not name its
        # last folder for a different day than its first.
        "issued_at": core.now_uk().isoformat(),
        "venues": venues,
        "queue": selected,
        "item": 0,
        "voucher": 0,
        "parts": [],
        # Whether a print sheet can be drawn in pieces and put back together.
        # Without PyMuPDF it cannot, and an event is then drawn in one go: a
        # slower run, and on the server one that can still be cut off, but a
        # whole sheet either way. Settled once, here, so a server that loses
        # the library mid-run does not start slicing sheets it cannot rejoin.
        "sliced": core.pdf_merge_available(),
        "results": [],
        "total": sum(session["requests"][i].count for i in selected),
        "done_vouchers": 0,
        "seconds": 0.0,
        "steps": 0,
        "finished": False,
    }
    save_job(job)
    return job, ""


def _slice_size(job: dict, per_page: int, remaining: int) -> int:
    """How many vouchers this request should draw.

    Sized from the rate this run has actually been managing, which includes
    whatever each slice costs to set up, so the estimate errs slow. That is the
    right direction to be wrong in: too small only costs a round trip.
    """
    # Nothing to size when the sheet cannot be put back together: the event is
    # drawn in one piece, which is the only way it comes out whole.
    if not job.get("sliced", True):
        return remaining
    done, seconds = job["done_vouchers"], job["seconds"]
    if done < per_page or seconds <= 0:
        # Nothing measured yet this run, so the figure the last one saved
        # stands in. That is what pace.json is for, and it lives with the data
        # rather than in the repository precisely because the office machine
        # and the server draw at different speeds.
        rate, ceiling = core.read_pace()["seconds_per_voucher"], FIRST_STEP_MAX_PAGES
    else:
        rate, ceiling = seconds / done, MAX_STEP_PAGES
    pages = int(STEP_TARGET_SECONDS / max(rate * per_page, 1e-6))
    return min(max(1, min(pages, ceiling)) * per_page, remaining)


def _run_step(job: dict, writer: core.PdfWriter, config: dict) -> dict:
    """Draw the next slice of the run. The writer is already open.

    Everything this touches is written down before it returns, so the run can be
    picked up from the next request whichever worker process answers it.
    """
    if job["finished"]:
        return job

    session = load_session(job["token"])
    if not session:
        raise RuntimeError("the upload behind this run has expired")

    per_page = int(config.get("vouchers_per_page") or 6)
    issued_at = core.read_stamp(job["issued_at"])
    req = session["requests"][job["queue"][job["item"]]]

    started = time.perf_counter()
    size = _slice_size(job, per_page, req.count - job["voucher"])
    vs = core.build_vouchers(req, job["venues"])
    # Drawn beside the slice and renamed over it, for the same reason the job
    # file is. Two workers can end up drawing the same slice at once: a request
    # the load balancer gave up on is still running, and the retry that follows
    # it computes the same name from the same position in the run. Renaming
    # means the loser's file is replaced whole rather than interleaved with the
    # winner's, so the sheet they both feed cannot end up part-written.
    part = JOBS_DIR / job["job"] / f"{job['item']:03d}-{job['voucher']:06d}.pdf"
    drawing = part.with_suffix(f".{os.getpid()}.tmp")
    writer.write(
        render_sheet(vs[job["voucher"]:job["voucher"] + size], config,
                     req.event_name),
        drawing)
    _replace(drawing, part)

    job["parts"].append(str(part))
    job["voucher"] += size
    job["done_vouchers"] += size
    job["seconds"] += time.perf_counter() - started
    job["steps"] += 1

    if job["voucher"] >= req.count:
        # The folder is made here, at the end, rather than when the event
        # started. A run abandoned halfway through an event, by closing the tab
        # or losing the network, used to leave an empty folder behind named
        # exactly like a real batch, and the next attempt then wrote "(2)"
        # beside it. One folder per request, with the ID in its name, because
        # this folder is what gets sent to whoever asked for the vouchers.
        out_dir = core.unique_output_dir(req.event_name, issued_at, req.dmu_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Named for the batch, because these files leave the folder: two
        # batches downloaded as two zips used to unpack into two files called
        # Print sheet.pdf.
        #
        # The page count is checked rather than assumed. It is the one thing
        # that would let a sheet go out short: the slices are separate files
        # written by separate requests, and a sheet quietly missing a page is
        # worse than a run that stops and says so.
        drawn = core.merge_pdfs([Path(p) for p in job["parts"]],
                                out_dir / core.batch_file_name(
                                    "Print sheet", req.event_name, req.dmu_id,
                                    ".pdf"))
        expected = -(-req.count // per_page)
        if drawn is not None and drawn != expected:
            raise RuntimeError(
                f"{req.event_name} came out {drawn} pages, not {expected}")
        core.write_batch_summary(
            out_dir / core.batch_file_name(
                "Batch summary", req.event_name, req.dmu_id, ".csv"),
            req, vs, job["issued_by"], issued_at, job["venues"])

        # No vendor sheet and no copy of the export in here. The vendor sheet is
        # the same handout for every batch in the run and goes to vendors rather
        # than to the requestor, so it is downloaded on its own from the done
        # page. The export copy was the whole uploaded file, so sending one
        # requestor their folder showed them every other request in it; the file
        # itself is still kept in Uploads for the audit trail.
        job["results"].append({
            "event_name": req.event_name,
            "dmu_id": req.dmu_id,
            "count": req.count,
            "value_display": req.value_display,
            "total_display": req.total_display,
            "first_code": vs[0].dmu_code,
            "last_code": vs[-1].dmu_code,
            # Per event now, because the export carries a date per row.
            "valid_until": core.format_uk_date(req.expiry_date),
            "event_date": core.format_uk_date(req.event_date),
            "folder": str(out_dir),
            "pages": -(-req.count // per_page),
        })
        job["item"] += 1
        job["voucher"] = 0
        job["parts"] = []

    if job["item"] >= len(job["queue"]):
        job["finished"] = True
        # Refresh the loose copy that lives with the records. Its own
        # try/except, because the vouchers have already been written by this
        # point: a failure here must not be reported as a failed run.
        try:
            writer.write(render_vendor_sheet(config), core.LOOSE_VENDOR_PDF)
        except Exception:
            traceback.print_exc()
        # What this run actually cost, so the next one's first slice is sized
        # from a real figure. Drawing time only: the browser's round trips and
        # the engine starting up for each slice are both outside the clock, and
        # what is left is the part that scales with the number of vouchers,
        # which is the part being asked for here.
        core.record_pace(job["done_vouchers"], job["seconds"])
        shutil.rmtree(JOBS_DIR / job["job"], ignore_errors=True)

    save_job(job)
    return job


def _job_state(job: dict) -> dict:
    """What the page needs to draw the bar. Never the folders: a half-finished
    run has nothing to offer yet, and the done page is where they appear."""
    return {
        "job": job["job"],
        "total": job["total"],
        "done": job["done_vouchers"],
        "events_done": len(job["results"]),
        "events": len(job["queue"]),
        "steps": job["steps"],
        "finished": job["finished"],
    }


def _step_failed(job: dict | None):
    """What to say when a slice will not draw.

    Deliberately not "nothing was written anywhere", which is what the old
    single-request handler said and which a sliced run can make untrue: batches
    finished before the one that failed are complete and on disk. Saying so is
    the difference between somebody reprinting one event and reprinting five.
    """
    traceback.print_exc()
    done = len(job["results"]) if job else 0
    where = ""
    if done:
        where = (f" {done} of the events were finished before it stopped and "
                 "their folders are complete, so only the rest need doing "
                 "again. Untick the ones already made before trying again, or "
                 "they will be drawn a second time into a second folder.")
    return _index_with_error(
        "Something went wrong while making the PDFs." + (where or
        " Nothing was written anywhere, so there is nothing to undo.") +
        " The details are in the black command window behind this page. If it "
        "mentions 'playwright' or 'chromium', close the app and run run.bat "
        "again.",
        token=(job or {}).get("token"))


@app.post("/generate")
def generate():
    """The whole run in one request.

    What a browser with no JavaScript still posts to, and what the office
    machine is perfectly happy with. One writer is opened for the lot, so
    nothing here costs more than it did before the run was sliced.

    A browser that can drive the slices itself does not come through here; see
    /generate/plan below. On the server this is the path that can still be cut
    off by the load balancer, and there is no way round that for a client that
    cannot ask for one piece at a time.
    """
    config = core.load_config()
    job, error = _plan_run(config)
    if job is None:
        return _index_with_error(error, token=request.form.get("token", ""))

    try:
        with core.PdfWriter() as writer:
            while not job["finished"]:
                _run_step(job, writer, config)
    except Exception:
        return _step_failed(job)

    return _done_page(job, config)


@app.post("/generate/plan")
def generate_plan():
    """Work out the run and hand it back for the browser to ask for in slices.

    JSON, and answers 200 with an `error` in it rather than a status code, so
    the page can put a rejected run's message where every other one goes.
    """
    config = core.load_config()
    job, error = _plan_run(config)
    if job is None:
        return jsonify({"error": error}), 200
    return jsonify({"state": _job_state(job)}), 200


@app.post("/generate/step")
def generate_step():
    """One slice of a run.

    `after` is where the browser thinks the run has got to. A step that has
    already been done answers with the state rather than drawing it twice,
    which is what makes retrying a request whose answer went missing safe.
    """
    job = load_job(request.form.get("job", ""))
    if not job:
        return jsonify({"error": "That run has expired. Start it again."}), 200

    try:
        after = int(request.form.get("after", "-1"))
    except ValueError:
        after = -1
    if after >= 0 and job["steps"] > after:
        return jsonify({"state": _job_state(job)}), 200

    config = core.load_config()
    try:
        with core.PdfWriter() as writer:
            _run_step(job, writer, config)
    except Exception:
        traceback.print_exc()
        return jsonify({"failed": True,
                        "events_done": len(job["results"]),
                        "state": _job_state(job)}), 200
    return jsonify({"state": _job_state(job)}), 200


@app.post("/generate/done")
def generate_done():
    """The finished page for a run the browser drove itself."""
    job = load_job(request.form.get("job", ""))
    if not job or not job["finished"]:
        return _index_with_error(
            "That run is no longer here to finish. The vouchers it had already "
            "made are in Output.", token=(job or {}).get("token"))
    return _done_page(job, core.load_config())


def _done_page(job: dict, config: dict):
    return render_template(
        "done.html",
        cfg=config,
        hosted=HOSTED,
        results=job["results"],
        venues=job["venues"],
        logos=logo_uris(),
        issued_by=job["issued_by"],
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


@app.get("/health")
def health():
    config = core.load_config()
    return jsonify({
        "qr_url_configured": core.qr_url_configured(config),
        "qr_url": core.qr_url(config),
        "qr": qr_quality(config),
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
