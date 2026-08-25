"""DMU Food & Drink voucher redemption site.

This is the thing a vendor's phone opens when they scan the QR code on a
voucher. The same QR code is on every voucher, so all it does is bring up this
page. What identifies the voucher is the number the vendor then types in.

The whole flow is three screens:

    sign in  ->  type the number  ->  green or red

Sign-in is once a shift, not once a customer, because there is a queue.

There is also an admin area for DMU Venues: import a batch from the generator,
search a voucher, undo a redemption made in error, and download everything as a
CSV to reconcile against the ledger.

Runs on PythonAnywhere. Start it locally with:  python app.py
"""

from __future__ import annotations

import csv
import io
import json
import os
import secrets
import sys
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, get_flashed_messages, redirect,
                   render_template, request, Response, session, url_for)

HERE = Path(__file__).resolve().parent

# refs.py and store.py are shared with the generator and live one level up.
# Appended, never inserted at the front: the generator folder also holds an
# app.py, and putting it ahead of this folder makes "import app" pick up the
# wrong one. That failure is silent and looks like every route disappearing.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(HERE.parent) not in sys.path:
    sys.path.append(str(HERE.parent))

import refs      # noqa: E402
import store     # noqa: E402
import settings  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.secret_key = settings.SECRET_KEY or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=settings.VENDOR_SESSION_HOURS)


def db():
    return store.init(settings.DB_PATH)

@app.before_request
def site_gate():
    """One browser password prompt in front of the entire site.

    Only active when DMU_SITE_PASSWORD is set, which is meant for while the
    site is being tested on a public address. Unset, this does nothing and the
    vendor sees the normal sign-in page.

    This sits in front of the vendor password rather than replacing it. It
    hides the site from anyone who stumbles on the URL; the vendor password is
    still what stops a redemption being recorded.
    """
    if not settings.SITE_PASSWORD:
        return None

    auth = request.authorization
    supplied_user = (auth.username or "") if auth else ""
    supplied_pass = (auth.password or "") if auth else ""

    # compare_digest on both, and both always evaluated, so neither the
    # username nor the password can be narrowed down by timing.
    user_ok = secrets.compare_digest(supplied_user, settings.SITE_USER)
    pass_ok = secrets.compare_digest(supplied_pass, settings.SITE_PASSWORD)
    if user_ok and pass_ok:
        return None

    return Response(
        "This site is not open yet.", 401,
        {"WWW-Authenticate": 'Basic realm="DMU vouchers"',
         "Cache-Control": "no-store"},
    )


@app.after_request
def no_indexing(response: Response) -> Response:
    """Keep the site out of search results.

    A voucher scheme has no reason to be findable, and a stale cached copy of
    a redemption screen is worse than useless.
    """
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response




# --------------------------------------------------------------------------
# Sign in
# --------------------------------------------------------------------------

def vendor_signed_in() -> bool:
    return bool(session.get("venue"))


def admin_signed_in() -> bool:
    return bool(session.get("admin"))


def require_vendor(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not vendor_signed_in():
            return redirect(url_for("signin", next=request.path))
        return view(*a, **kw)
    return wrapped


def require_admin(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not admin_signed_in():
            return redirect(url_for("admin_signin"))
        return view(*a, **kw)
    return wrapped


@app.get("/signin")
def signin():
    if settings.configured():
        return render_template("unconfigured.html",
                               missing=settings.configured()), 503
    if vendor_signed_in():
        return redirect(url_for("home"))
    return render_template("signin.html", venues=settings.VENUES)


@app.post("/signin")
def signin_post():
    if settings.configured():
        abort(503)

    venue = (request.form.get("venue") or "").strip()
    password = request.form.get("password") or ""

    if venue not in settings.VENUES:
        return render_template("signin.html", venues=settings.VENUES,
                               error="Choose which venue you are."), 400

    # compare_digest so a wrong password takes the same time as a right one
    if not secrets.compare_digest(password, settings.VENDOR_PASSWORD):
        return render_template("signin.html", venues=settings.VENUES,
                               venue=venue,
                               error="That password is not right. Check with "
                                     "the university if you have lost it."), 401

    session.permanent = True
    session["venue"] = venue
    return redirect(request.args.get("next") or url_for("home"))


@app.post("/signout")
def signout():
    session.pop("venue", None)
    return redirect(url_for("signin"))


# --------------------------------------------------------------------------
# Redeeming
# --------------------------------------------------------------------------

@app.get("/")
def root():
    """Where the QR code lands, and where anybody typing the address lands.

    A vendor mid-shift goes straight to the number entry. The QR code is the
    same on every voucher, so this route is hit once per customer, and putting a
    menu in front of it would mean a tap per customer at a till with a queue.

    Only somebody not signed in sees the choice of vendor or admin.
    """
    if settings.configured():
        return render_template("unconfigured.html",
                               missing=settings.configured()), 503
    if vendor_signed_in():
        return redirect(url_for("home"))
    if admin_signed_in():
        return redirect(url_for("admin"))
    return render_template("choose.html")


@app.get("/home")
@require_vendor
def home():
    return render_template("enter.html", venue=session["venue"])


def recent_failures(conn, venue: str) -> int:
    since = (datetime.now()
             - timedelta(minutes=settings.FAILURE_WINDOW_MINUTES)).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM lookup_failures WHERE venue = ? AND at > ?",
        (venue, since),
    ).fetchone()[0]


def note_failure(conn, tried: str, reason: str, venue: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO lookup_failures (tried, reason, client, venue, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tried[:40], reason, request.headers.get(
                "X-Forwarded-For", request.remote_addr or "")[:60],
             venue, store.now()),
        )


@app.post("/check")
@require_vendor
def check():
    """Look a number up and say plainly whether it can be redeemed.

    Nothing is written by a lookup. The vendor sees the value first and presses
    Redeem separately, so a mistyped number cannot burn a voucher.
    """
    venue = session["venue"]
    typed = (request.form.get("reference") or "").strip()
    conn = db()

    try:
        if recent_failures(conn, venue) >= settings.MAX_FAILURES:
            return render_template(
                "result.html", venue=venue, state="blocked",
                heading="Too many wrong numbers",
                detail=f"There have been {settings.MAX_FAILURES} failed "
                       f"lookups from this venue in the last "
                       f"{settings.FAILURE_WINDOW_MINUTES} minutes. Wait a few "
                       f"minutes and try again, or ring DMU Venues.",
            ), 429

        reference = refs.normalise_typed(typed) or refs.normalise_legacy(typed)

        if not reference:
            note_failure(conn, typed, "not a valid number", venue)
            return render_template(
                "result.html", venue=venue, state="bad", typed=typed,
                heading="That is not a voucher number",
                detail="Check the red box at the bottom of the voucher and "
                       "type it again. It is nine digits, like "
                       f"{refs.make_reference(48217390)}.",
            ), 404

        voucher = store.get_voucher(conn, reference)
        if not voucher:
            note_failure(conn, reference, "not issued", venue)
            return render_template(
                "result.html", venue=venue, state="bad", typed=reference,
                heading="This voucher is not recognised",
                detail="The number reads correctly but no voucher was issued "
                       "with it. Do not accept this voucher. If the customer "
                       "disputes it, ring DMU Venues.",
            ), 404

        return render_template("result.html", venue=venue,
                               **assess(conn, voucher))
    finally:
        conn.close()


def assess(conn, voucher) -> dict:
    """Everything the vendor needs to decide, in one place.

    Called by the lookup and again at the moment of redeeming, so the two can
    never disagree about whether a voucher was good.
    """
    reference = voucher["reference"]
    ctx = {
        "reference": reference,
        "voucher": voucher,
        "value": f"£{voucher['value_pence'] / 100:,.2f}",
        "valid_until_display": uk_date(voucher["valid_until"]),
    }

    if voucher["voided_at"]:
        return {**ctx, "state": "bad", "heading": "This voucher has been cancelled",
                "detail": (voucher["void_reason"] or
                           "It was cancelled by the university.") +
                          " Do not accept it."}

    existing = store.live_redemption(conn, reference)
    if existing:
        when = stamp(existing["redeemed_at"])
        return {**ctx, "state": "used",
                "heading": "Already redeemed",
                "detail": f"This voucher was redeemed at "
                          f"{existing['venue']} on {when}. Do not accept it "
                          f"again."}

    if expired(voucher["valid_until"]):
        return {**ctx, "state": "expired",
                "heading": "This voucher has expired",
                "detail": f"It was only valid until "
                          f"{uk_date(voucher['valid_until'])}. Do not accept it."}

    return {**ctx, "state": "good",
            "heading": f"Good for {ctx['value']}",
            "detail": f"Valid until {ctx['valid_until_display']}. Press Redeem, "
                      f"then serve the order."}


@app.post("/redeem")
@require_vendor
def redeem():
    venue = session["venue"]
    reference = refs.normalise_typed(request.form.get("reference") or "")
    if not reference:
        abort(400)

    conn = db()
    try:
        voucher = store.get_voucher(conn, reference)
        if not voucher:
            abort(404)

        # Assessed again here rather than trusting the screen the vendor is
        # looking at. Two tills can be on the same voucher at the same time.
        verdict = assess(conn, voucher)
        if verdict["state"] != "good":
            return render_template("result.html", venue=venue, **verdict), 409

        try:
            with conn:
                conn.execute(
                    "INSERT INTO redemptions (reference, venue, redeemed_at, "
                    "served_by) VALUES (?, ?, ?, ?)",
                    (reference, venue, store.now(),
                     (request.form.get("served_by") or "").strip()[:60]),
                )
        except Exception:
            # The unique index caught a second redemption that landed in the
            # gap between the check and the write. Show the truth, not an error.
            return render_template("result.html", venue=venue,
                                   **assess(conn, voucher)), 409

        return render_template("done.html", venue=venue, reference=reference,
                               value=verdict["value"],
                               event=voucher["event_name"])
    finally:
        conn.close()


@app.get("/setup")
def setup():
    """Says what is wrong and what to do about it, in words.

    Readable by anyone while the site is not yet configured, because that is
    exactly when somebody is locked out and needs to know why. Once the
    passwords are set it needs the admin password like everything else.

    It never shows a value, only whether one is set and where it came from.
    Knowing that DMU_ADMIN_PASSWORD is being read from the Web tab rather than
    the .env file is the difference between a two minute fix and an afternoon.
    """
    missing = settings.configured()

    # Readable without a password in the two states that lock somebody out:
    # nothing is set, or something is set twice and they cannot tell which value
    # is live. The second is the nastier one, because the sign-in page looks
    # perfectly normal and simply rejects what you type.
    locked_out = bool(missing) or bool(settings.SHADOWED)
    signed_in = admin_signed_in()
    if not locked_out and not signed_in:
        return redirect(url_for("admin_signin", next=url_for("setup")))

    rows = []
    for name, what in (
        ("DMU_VENDOR_PASSWORD", "What vendors type at the venue"),
        ("DMU_ADMIN_PASSWORD", "What DMU Venues types here"),
        ("DMU_SECRET_KEY", "Signs the sign-in cookie"),
        ("DMU_DB_PATH", "Where redemptions are stored"),
        ("DMU_SITE_PASSWORD", "Optional. Hides the whole site while testing"),
    ):
        rows.append({
            "name": name,
            "what": what,
            "set": bool(os.environ.get(name)),
            "source": settings.source(name),
            "shadowed": name in settings.SHADOWED,
            "optional": name in ("DMU_SITE_PASSWORD", "DMU_DB_PATH"),
        })

    # Can the database actually be written? A path that looks right but is not
    # writable fails at the worst moment, which is a vendor at a till.
    db_ok, db_note, counts = True, "", {}
    try:
        conn = db()
        try:
            counts = {
                "vouchers": conn.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0],
                "redemptions": conn.execute(
                    "SELECT COUNT(*) FROM redemptions WHERE reversed_at IS NULL"
                ).fetchone()[0],
            }
        finally:
            conn.close()
    except Exception as exc:
        db_ok = False
        db_note = str(exc)

    todo = []
    for row in rows:
        if not row["set"] and not row["optional"]:
            todo.append(f"Set {row['name']} in the Web tab, then press Reload.")
        if row["shadowed"]:
            todo.append(
                f"{row['name']} is set in two places and the .env file is being "
                f"ignored. Use the Web tab value, or delete that row from the "
                f"Web tab so the file wins.")
    if not db_ok:
        todo.append("The database cannot be written. See the note below.")
    if not request.is_secure:
        todo.append("Turn on Force HTTPS on the Web tab, then press Reload. "
                    "Without it passwords cross the network in clear text.")
    if db_ok and not counts.get("vouchers"):
        todo.append("No batch imported yet, so no voucher will be recognised. "
                    "Import one in the admin area.")

    return render_template(
        "setup.html", rows=rows, todo=todo, db_ok=db_ok, db_note=db_note,
        counts=counts, secure=request.is_secure,
        dotenv_found=settings.DOTENV_FOUND,
        venues=settings.VENUES, locked_out=locked_out,
        # Operational detail only for somebody who has actually signed in.
        detailed=signed_in,
    )


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

@app.get("/admin/signin")
def admin_signin():
    if settings.configured():
        return render_template("unconfigured.html",
                               missing=settings.configured()), 503
    return render_template("admin_signin.html")


@app.post("/admin/signin")
def admin_signin_post():
    if settings.configured():
        abort(503)
    password = request.form.get("password") or ""
    if not secrets.compare_digest(password, settings.ADMIN_PASSWORD):
        return render_template("admin_signin.html",
                               error="That password is not right."), 401
    session.permanent = True
    session["admin"] = True
    return redirect(url_for("admin"))


@app.post("/admin/signout")
def admin_signout():
    session.pop("admin", None)
    return redirect(url_for("admin_signin"))


@app.get("/admin")
@require_admin
def admin():
    conn = db()
    try:
        totals = conn.execute(
            "SELECT COUNT(*) AS issued, "
            " COALESCE(SUM(value_pence), 0) AS value_pence FROM vouchers"
        ).fetchone()
        redeemed = conn.execute(
            "SELECT COUNT(*) AS n FROM redemptions WHERE reversed_at IS NULL"
        ).fetchone()["n"]
        redeemed_value = conn.execute(
            "SELECT COALESCE(SUM(v.value_pence), 0) AS p FROM redemptions r "
            "JOIN vouchers v ON v.reference = r.reference "
            "WHERE r.reversed_at IS NULL"
        ).fetchone()["p"]

        by_venue = conn.execute(
            "SELECT r.venue, COUNT(*) AS n, "
            " COALESCE(SUM(v.value_pence), 0) AS p FROM redemptions r "
            "JOIN vouchers v ON v.reference = r.reference "
            "WHERE r.reversed_at IS NULL GROUP BY r.venue ORDER BY n DESC"
        ).fetchall()

        by_batch = conn.execute(
            "SELECT v.batch, v.event_name, COUNT(*) AS issued, "
            " SUM(CASE WHEN r.id IS NOT NULL THEN 1 ELSE 0 END) AS redeemed, "
            " MAX(v.value_pence) AS value_pence, MAX(v.valid_until) AS valid_until "
            "FROM vouchers v "
            "LEFT JOIN redemptions r "
            "  ON r.reference = v.reference AND r.reversed_at IS NULL "
            "GROUP BY v.batch, v.event_name ORDER BY v.batch DESC"
        ).fetchall()

        recent = conn.execute(
            "SELECT r.*, v.event_name, v.value_pence FROM redemptions r "
            "LEFT JOIN vouchers v ON v.reference = r.reference "
            "ORDER BY r.id DESC LIMIT 25"
        ).fetchall()

        failures = conn.execute(
            "SELECT COUNT(*) AS n FROM lookup_failures WHERE at > ?",
            ((datetime.now() - timedelta(days=1)).isoformat(),),
        ).fetchone()["n"]

        return render_template(
            "admin.html", totals=totals, redeemed=redeemed,
            redeemed_value=redeemed_value, by_venue=by_venue,
            by_batch=by_batch, recent=recent, failures=failures,
            money=money, stamp=stamp, uk_date=uk_date,
            messages=get_flashed_messages(with_categories=True),
        )
    finally:
        conn.close()


@app.post("/admin/import")
@require_admin
def admin_import():
    """Take an 'Import to redemption site.json' from the generator.

    Importing the same file twice is harmless: vouchers already present are
    left exactly as they are, redemptions and all.
    """
    upload = request.files.get("batch")
    if not upload or not upload.filename:
        flash("Choose the Import to redemption site.json file first.", "error")
        return redirect(url_for("admin"))

    try:
        payload = json.loads(upload.read().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        flash("That file could not be read. It should be the "
              "'Import to redemption site.json' from the voucher generator.",
              "error")
        return redirect(url_for("admin"))

    if payload.get("format") != "dmu-voucher-batch":
        flash("That is not a voucher batch file. Look for "
              "'Import to redemption site.json' in the batch folder.", "error")
        return redirect(url_for("admin"))

    references = payload.get("references") or []
    bad = [r for r in references if not refs.normalise_typed(r)]
    if bad:
        flash(f"{len(bad)} of the numbers in that file are not valid voucher "
              f"numbers, so nothing was imported. First one: {bad[0]}", "error")
        return redirect(url_for("admin"))

    conn = db()
    try:
        added = store.record_vouchers(conn, [{
            "reference": r,
            "batch": payload.get("batch", ""),
            "event_name": payload.get("event_name", ""),
            "cost_centre": payload.get("cost_centre", ""),
            "value_pence": payload.get("value_pence", 0),
            "valid_until": payload.get("valid_until", ""),
            "event_date": payload.get("event_date", ""),
            "issued_at": payload.get("issued_at", store.now()),
            "issued_by": payload.get("issued_by", ""),
        } for r in references])
    finally:
        conn.close()

    skipped = len(references) - added
    note = f", {skipped} already imported" if skipped else ""
    flash(f"Batch {payload.get('batch', '')} imported: {added} voucher"
          f"{'' if added == 1 else 's'} for {payload.get('event_name', '')}"
          f"{note}.", "ok")
    return redirect(url_for("admin"))


@app.get("/admin/voucher")
@require_admin
def admin_voucher():
    typed = (request.args.get("reference") or "").strip()
    reference = refs.normalise_typed(typed) or refs.normalise_legacy(typed)
    if not reference:
        flash(f"'{typed}' is not a voucher number.", "error")
        return redirect(url_for("admin"))

    conn = db()
    try:
        voucher = store.get_voucher(conn, reference)
        if not voucher:
            flash(f"{reference} has not been issued, or its batch has not been "
                  f"imported yet.", "error")
            return redirect(url_for("admin"))
        history = conn.execute(
            "SELECT * FROM redemptions WHERE reference = ? ORDER BY id DESC",
            (reference,),
        ).fetchall()
        return render_template("admin_voucher.html", voucher=voucher,
                               history=history, money=money, stamp=stamp,
                               uk_date=uk_date, expired=expired(voucher["valid_until"]),
                               live=store.live_redemption(conn, reference))
    finally:
        conn.close()


@app.post("/admin/reverse")
@require_admin
def admin_reverse():
    """Undo a redemption. The row stays and is marked reversed, so the record
    shows that it happened and was undone rather than showing nothing."""
    reference = refs.normalise_typed(request.form.get("reference") or "")
    reason = (request.form.get("reason") or "").strip()
    if not reference:
        abort(400)
    if not reason:
        flash("Say why it is being undone. It goes on the record.", "error")
        return redirect(url_for("admin_voucher", reference=reference))

    conn = db()
    try:
        cur = conn.execute(
            "UPDATE redemptions SET reversed_at = ?, reversed_by = ?, "
            "reversal_reason = ? WHERE reference = ? AND reversed_at IS NULL",
            (store.now(), "admin", reason[:200], reference),
        )
        conn.commit()
        if cur.rowcount:
            flash(f"{reference} is redeemable again. The reversal is on the "
                  f"record.", "ok")
        else:
            flash(f"{reference} had no live redemption to undo.", "error")
    finally:
        conn.close()
    return redirect(url_for("admin_voucher", reference=reference))


@app.post("/admin/void")
@require_admin
def admin_void():
    """Cancel a voucher, for one reported lost or printed in error."""
    reference = refs.normalise_typed(request.form.get("reference") or "")
    reason = (request.form.get("reason") or "").strip()
    undo = request.form.get("undo") == "1"
    if not reference:
        abort(400)

    conn = db()
    try:
        if undo:
            conn.execute("UPDATE vouchers SET voided_at = NULL, "
                         "void_reason = NULL WHERE reference = ?", (reference,))
            flash(f"{reference} is back in use.", "ok")
        else:
            if not reason:
                flash("Say why it is being cancelled.", "error")
                return redirect(url_for("admin_voucher", reference=reference))
            conn.execute("UPDATE vouchers SET voided_at = ?, void_reason = ? "
                         "WHERE reference = ?",
                         (store.now(), reason[:200], reference))
            flash(f"{reference} is cancelled and will be refused.", "ok")
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin_voucher", reference=reference))


@app.get("/admin/redemptions.csv")
@require_admin
def admin_csv():
    """Everything, for reconciling against the generator's ledger."""
    conn = db()
    try:
        rows = conn.execute(
            "SELECT v.reference, v.batch, v.event_name, v.cost_centre, "
            "  v.value_pence, v.valid_until, v.event_date, v.issued_at, "
            "  v.voided_at, r.venue, r.redeemed_at, r.served_by, "
            "  r.reversed_at, r.reversal_reason "
            "FROM vouchers v "
            "LEFT JOIN redemptions r "
            "  ON r.reference = v.reference AND r.reversed_at IS NULL "
            "ORDER BY v.batch, v.reference"
        ).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Voucher number", "Batch", "Event", "Cost centre",
                     "Value", "Valid until", "Event date", "Issued",
                     "Cancelled", "Redeemed at", "Redeemed on", "Served by",
                     "Status"])
    for r in rows:
        if r["voided_at"]:
            status = "Cancelled"
        elif r["redeemed_at"]:
            status = "Redeemed"
        elif expired(r["valid_until"]):
            status = "Expired unused"
        else:
            status = "Unused"
        writer.writerow([
            r["reference"], r["batch"], r["event_name"], r["cost_centre"],
            money(r["value_pence"]), uk_date(r["valid_until"]),
            uk_date(r["event_date"]), r["issued_at"], r["voided_at"] or "",
            r["venue"] or "", stamp(r["redeemed_at"]) if r["redeemed_at"] else "",
            r["served_by"] or "", status,
        ])

    stampname = datetime.now().strftime("%Y-%m-%d")
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="DMU voucher redemptions {stampname}.csv"'},
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def money(pence) -> str:
    return f"£{(pence or 0) / 100:,.2f}"


def uk_date(value: str) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value).strftime("%d %B %Y").lstrip("0")
    except ValueError:
        return value


def stamp(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d %B %Y at %H:%M").lstrip("0")
    except ValueError:
        return value


def expired(valid_until: str) -> bool:
    if not valid_until:
        return False
    try:
        return date.fromisoformat(valid_until) < date.today()
    except ValueError:
        return False


@app.get("/health")
def health():
    conn = db()
    try:
        return {
            "ok": not settings.configured(),
            "missing_settings": settings.configured(),
            "vouchers": conn.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0],
            "redeemed": conn.execute(
                "SELECT COUNT(*) FROM redemptions WHERE reversed_at IS NULL"
            ).fetchone()[0],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    missing = settings.configured()
    print()
    print("  DMU voucher redemption site")
    if missing:
        print("  Not ready yet. These still need setting: " + ", ".join(missing))
        print("  Put them in redeem/.env. See redeem/.env.example.")
    print(f"  Database: {settings.DB_PATH}")
    print("  Open this in your browser:  http://127.0.0.1:5058")
    print()
    app.run(host="127.0.0.1", port=5058, debug=False)
