"""Core voucher logic for the DMU Food & Drink voucher generator.

Deliberately free of Flask imports so it can be driven from a script or tested
on its own. The app layer only handles the browser side of things.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path

import segno

APP_DIR = Path(__file__).resolve().parent

# Everything the app writes lives under here: the ledger, the finished batches,
# the uploaded CSVs. On a server point DMU_DATA_DIR at a folder OUTSIDE the
# repository, or deploying a new version takes the record of every voucher ever
# issued with it. Unset, which is how the office machine runs, it all sits next
# to the app exactly as it always has.
DATA_DIR = Path(os.environ.get("DMU_DATA_DIR") or APP_DIR)

CONFIG_PATH = APP_DIR / "config.json"
LEDGER_PATH = DATA_DIR / "Ledger" / "issued_vouchers.csv"
OUTPUT_DIR = DATA_DIR / "Output"
ASSETS_DIR = APP_DIR / "assets"

# How long the last print run took, so the next one can say how far through it
# is. It lives with the data rather than in the repository: the office machine
# draws with Chromium and the server with WeasyPrint, on hardware nothing here
# knows about, so a figure measured on one is no guide to the other.
PACE_PATH = DATA_DIR / "pace.json"

# What to assume before this copy of the app has ever finished a run. Slower
# than Chromium manages and quicker than a big WeasyPrint sheet, and wrong only
# once: the first completed run replaces it with the truth.
DEFAULT_PACE = {"fixed_seconds": 4.0, "seconds_per_voucher": 0.15}

# The loose copy of the vendor sheet, refreshed after every run so the current
# one is always to hand without digging through Output.
LOOSE_VENDOR_PDF = DATA_DIR / "Vendor instructions.pdf"

PLACEHOLDER_QR_URL = "PASTE THE MICROSOFT FORM ADDRESS HERE"

LEDGER_FIELDS = [
    # The code printed on the voucher, and the only thing identifying it. The
    # random DMU-482-173-906 reference this replaced is gone, but rows written
    # before that carry one and _migrate_ledger_header keeps their column: those
    # numbers are on paper in circulation and are not ours to drop.
    "dmu_code",
    "batch",
    "event_name",
    "cost_centre",
    "value_pence",
    "valid_until",
    "event_date",
    "budget_approver",
    "lead_contact",
    "issued_at",
    "issued_by",
    "output_folder",
]


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def qr_url(config: dict) -> str:
    """The address every voucher's QR code opens. No trailing slash.

    Reads qr_url, falling back to redeem_url, which is what this was called
    while the QR still pointed at our own redemption site. A config.json that
    has not been updated therefore keeps working.
    """
    raw = config.get("qr_url") or config.get("redeem_url") or ""
    return raw.strip().rstrip("/")


def qr_url_configured(config: dict) -> bool:
    url = qr_url(config)
    return (bool(url)
            and url != PLACEHOLDER_QR_URL
            and url.lower().startswith("http"))


# --------------------------------------------------------------------------
# Money helpers
# --------------------------------------------------------------------------

def parse_money(raw: str) -> int | None:
    """'£6.00' -> 600 pence. Returns None if nothing numeric is present."""
    if raw is None:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(raw)).strip()
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        return int(round(float(cleaned) * 100))
    except ValueError:
        return None


def format_money(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


def parse_int(raw: str) -> int | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[^0-9\-]", "", str(raw)).strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_uk_date(raw: str) -> str:
    """'30/09/2026' -> '2026-09-30'. Everything downstream works in ISO.

    The export writes UK order, and Excel sometimes hands back ISO instead after
    somebody has opened and resaved the file, so both are accepted. Anything else
    comes back empty rather than guessed: a wrong expiry date on a voucher is
    worse than a row the app refuses to print.
    """
    if not raw:
        return ""
    cleaned = str(raw).strip()
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(cleaned, pattern).date().isoformat()
        except ValueError:
            continue
    return ""


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------

HEADER_ALIASES = {
    "id": "dmu_id",
    "event name": "event_name",
    "number of vouchers": "count",
    "value per voucher": "value",
    "total value": "total_value",
    "cost centre": "cost_centre",
    "cost center": "cost_centre",
    "budget approver": "budget_approver",
    "budget approver email": "budget_approver_email",
    "lead contact": "lead_contact",
    "lead contact email": "lead_contact_email",
    "status": "status",
    "expiry date": "expiry_date",
    "event date": "event_date",
}


@dataclass
class VoucherRequest:
    row_number: int
    event_name: str
    count: int
    value_pence: int
    total_value_pence: int | None
    cost_centre: str
    budget_approver: str
    budget_approver_email: str
    lead_contact: str
    lead_contact_email: str
    status: str
    # From the export, ISO or empty. The dates are the file's to decide: there is
    # no date field on the form, so a mixed drop cannot be given one date by hand.
    dmu_id: str = ""
    expiry_date: str = ""
    event_date: str = ""
    warnings: list[str] = field(default_factory=list)

    def dmu_code_for(self, ticket: int) -> str:
        """DMU's own reference for one voucher: the request ID and the ticket number.

        Padded to the width of the count, so a run of 100 reads 12-001 to 12-100
        and sorts in a spreadsheet. Blank when the export has no ID, which is
        what old files and hand-made ones look like.
        """
        if not self.dmu_id:
            return ""
        return f"{self.dmu_id}-{ticket:0{len(str(self.count))}d}"

    @property
    def key(self) -> str:
        return "|".join([
            self.event_name.strip().lower(),
            self.cost_centre.strip(),
            str(self.value_pence),
            str(self.count),
        ])

    @property
    def value_display(self) -> str:
        return format_money(self.value_pence)

    @property
    def total_display(self) -> str:
        return format_money(self.value_pence * self.count)

    @property
    def expiry_display(self) -> str:
        return format_uk_date(self.expiry_date)

    @property
    def event_date_display(self) -> str:
        return format_uk_date(self.event_date)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["value_display"] = self.value_display
        d["total_display"] = self.total_display
        d["expiry_display"] = self.expiry_display
        d["event_date_display"] = self.event_date_display
        return d


@dataclass
class ParseResult:
    requests: list[VoucherRequest] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalise_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """Map the CSV's own header text to our internal names.

    The export has quirks such as a trailing space in 'Event Name ', so match on
    the stripped, lower-cased header.
    """
    mapping: dict[str, str] = {}
    for name in fieldnames or []:
        if name is None:
            continue
        key = re.sub(r"\s+", " ", name).strip().lower()
        if key in HEADER_ALIASES:
            mapping[name] = HEADER_ALIASES[key]
    return mapping


def parse_csv(text: str) -> ParseResult:
    result = ParseResult()

    # Strip a UTF-8 BOM if Excel added one.
    if text.startswith("﻿"):
        text = text[1:]

    reader = csv.DictReader(io.StringIO(text))
    mapping = _normalise_headers(reader.fieldnames)

    required = {"event_name", "count", "value", "status"}
    missing = required - set(mapping.values())
    if missing:
        pretty = ", ".join(sorted(missing)).replace("_", " ")
        result.errors.append(
            f"This file does not look like the voucher approval export. "
            f"Missing column(s): {pretty}. Columns found: "
            f"{', '.join(n.strip() for n in (reader.fieldnames or []) if n)}"
        )
        return result

    def get(row: dict, field_name: str) -> str:
        for source, target in mapping.items():
            if target == field_name:
                return (row.get(source) or "").strip()
        return ""

    row_number = 1  # header is row 1
    for row in reader:
        row_number += 1
        if not any((v or "").strip() for v in row.values()):
            continue  # blank trailing line

        event_name = re.sub(r"\s+", " ", get(row, "event_name")).strip()
        status = get(row, "status")
        count = parse_int(get(row, "count"))
        value_pence = parse_money(get(row, "value"))
        total_pence = parse_money(get(row, "total_value"))
        expiry_date = parse_uk_date(get(row, "expiry_date"))
        event_date = parse_uk_date(get(row, "event_date"))
        dmu_id = get(row, "dmu_id")

        label = event_name or f"row {row_number}"

        if status.strip().lower() != "approved":
            result.skipped.append({
                "row_number": row_number,
                "event_name": label,
                "reason": f"status is '{status or 'blank'}', not Approved",
            })
            continue

        if not event_name:
            result.skipped.append({
                "row_number": row_number,
                "event_name": label,
                "reason": "no event name",
            })
            continue

        if not count or count < 1:
            result.skipped.append({
                "row_number": row_number,
                "event_name": label,
                "reason": f"number of vouchers is '{get(row, 'count') or 'blank'}'",
            })
            continue

        if value_pence is None or value_pence < 1:
            result.skipped.append({
                "row_number": row_number,
                "event_name": label,
                "reason": f"value per voucher is '{get(row, 'value') or 'blank'}'",
            })
            continue

        # The ID is half the code in the red box, and that code is now the only
        # thing identifying a voucher. Without an ID the box prints empty, which
        # is a voucher nobody can record a redemption against, so the row stops
        # here rather than producing paper that cannot be reconciled.
        if not dmu_id:
            result.skipped.append({
                "row_number": row_number,
                "event_name": label,
                "reason": "no ID in the file, and the ID is half the voucher code",
            })
            continue

        # The expiry is the file's to supply and there is no field on the form to
        # fill one in with. A voucher printed without one never expires, and the
        # vendor sheet tells vendors to refuse an expired voucher, so a missing
        # date has to stop the row rather than be invented here.
        if not expiry_date:
            raw_expiry = get(row, "expiry_date")
            result.skipped.append({
                "row_number": row_number,
                "event_name": label,
                "reason": (f"expiry date is '{raw_expiry}', which is not a date"
                           if raw_expiry else
                           "no expiry date in the file. Add one and drop it in again"),
            })
            continue

        req = VoucherRequest(
            row_number=row_number,
            event_name=event_name,
            count=count,
            value_pence=value_pence,
            total_value_pence=total_pence,
            cost_centre=get(row, "cost_centre"),
            budget_approver=get(row, "budget_approver"),
            budget_approver_email=get(row, "budget_approver_email"),
            lead_contact=get(row, "lead_contact"),
            lead_contact_email=get(row, "lead_contact_email"),
            status=status,
            dmu_id=dmu_id,
            expiry_date=expiry_date,
            event_date=event_date,
        )

        raw_event_date = get(row, "event_date")
        if raw_event_date and not event_date:
            req.warnings.append(
                f"Event date in the file is '{raw_event_date}', which is not a "
                f"date, so it has been left off the voucher."
            )

        if total_pence is not None and total_pence != count * value_pence:
            req.warnings.append(
                f"Total value in the file is {format_money(total_pence)} but "
                f"{count} x {format_money(value_pence)} is "
                f"{format_money(count * value_pence)}. Check before printing."
            )

        if count > 200:
            req.warnings.append(
                f"{count} vouchers is a lot. That is "
                f"{-(-count // 6)} pages of paper."
            )

        result.requests.append(req)

    # Collapse duplicates of the same request. The export can show a row per
    # status change, so keep the first Approved one and note the rest.
    deduped: list[VoucherRequest] = []
    seen: dict[str, VoucherRequest] = {}
    for req in result.requests:
        if req.key in seen:
            result.skipped.append({
                "row_number": req.row_number,
                "event_name": req.event_name,
                "reason": f"duplicate of row {seen[req.key].row_number} in this file",
            })
            continue
        seen[req.key] = req
        deduped.append(req)
    result.requests = deduped

    # Two different requests sharing an ID would print two different events'
    # vouchers under the same codes, which is unreconcilable. The export should
    # never do this, so say so loudly rather than quietly printing it.
    by_id: dict[str, list[VoucherRequest]] = {}
    for req in result.requests:
        by_id.setdefault(req.dmu_id, []).append(req)
    for dmu_id, group in by_id.items():
        if len(group) < 2:
            continue
        rows = ", ".join(str(r.row_number) for r in group)
        for req in group:
            req.warnings.append(
                f"ID {dmu_id} is on more than one row in this file (rows {rows}). "
                f"Those events would print vouchers carrying the same codes. "
                f"Tick only one of them."
            )

    if not result.requests and not result.errors:
        result.errors.append(
            "No approved requests found in this file. Vouchers are only "
            "produced once a request shows as Approved."
        )

    return result


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

def read_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    with open(LEDGER_PATH, newline="", encoding="utf-8-sig") as fh:
        return [row for row in csv.DictReader(fh)]


def _next_batch_number(ledger: list[dict]) -> int:
    highest = 0
    for row in ledger:
        n = parse_int(row.get("batch", ""))
        if n and n > highest:
            highest = n
    return highest + 1


def previous_issue(request: VoucherRequest, ledger: list[dict] | None = None) -> dict | None:
    """Has this exact request been issued before? Guards against a re-drop."""
    ledger = read_ledger() if ledger is None else ledger
    matches: dict[str, list[dict]] = {}
    for row in ledger:
        key = "|".join([
            (row.get("event_name") or "").strip().lower(),
            (row.get("cost_centre") or "").strip(),
            (row.get("value_pence") or "").strip(),
        ])
        matches.setdefault(key, []).append(row)

    key = "|".join([
        request.event_name.strip().lower(),
        request.cost_centre.strip(),
        str(request.value_pence),
    ])
    rows = matches.get(key)
    if not rows:
        return None

    by_batch: dict[str, list[dict]] = {}
    for row in rows:
        by_batch.setdefault(row.get("batch", ""), []).append(row)

    for batch, batch_rows in sorted(by_batch.items(), key=lambda kv: parse_int(kv[0]) or 0, reverse=True):
        if len(batch_rows) == request.count:
            issued_at = (batch_rows[0].get("issued_at") or "").split("T")[0]
            return {
                "batch": batch,
                "count": len(batch_rows),
                "issued_at": issued_at,
                # dmu_code on anything issued since the codes changed, and the
                # old random reference on rows written before that, which are
                # still the numbers printed on those vouchers.
                "codes": [(r.get("dmu_code") or r.get("voucher_code") or "")
                          for r in batch_rows],
                "output_folder": batch_rows[0].get("output_folder", ""),
            }
    return None


def _migrate_ledger_header() -> list[str]:
    """Bring an older ledger up to the current columns, and say what to write with.

    append_ledger only writes a header when the file is new, so adding a column
    to LEDGER_FIELDS would otherwise append wider rows underneath the old,
    narrower header. Every reader of the file, this app included, would then
    misread the extra value. The fix has to happen before the first append, and
    it has to happen on the server, where the ledger already has real rows in it.

    Columns the file has that this version does not know about are kept rather
    than dropped, so a ledger written by a newer copy of the app is never
    silently narrowed by an older one.
    """
    if not LEDGER_PATH.exists():
        return list(LEDGER_FIELDS)

    with open(LEDGER_PATH, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        existing = [c for c in (reader.fieldnames or []) if c]
        if existing == LEDGER_FIELDS:
            return list(LEDGER_FIELDS)
        rows = list(reader)

    fields = LEDGER_FIELDS + [c for c in existing if c not in LEDGER_FIELDS]

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    backup = LEDGER_PATH.with_name(f"{LEDGER_PATH.stem}.bak-{stamp}{LEDGER_PATH.suffix}")
    shutil.copy2(LEDGER_PATH, backup)

    with open(LEDGER_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (row.get(k) or "") for k in fields})
    return fields


def append_ledger(rows: list[dict]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LEDGER_PATH.exists()
    fields = list(LEDGER_FIELDS) if new_file else _migrate_ledger_header()
    with open(LEDGER_PATH, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


# --------------------------------------------------------------------------
# QR code
# --------------------------------------------------------------------------

# The code is printed once, on the vendor instruction sheet, not on every
# voucher. An A4 sheet has room for a big one, which is the whole reason it was
# moved there: 45mm survives a bad phone photo and a heavy-handed printer.
# Below roughly half a millimetre per module, toner spread on ordinary office
# paper starts to defeat phone cameras, so we warn rather than let it go out.
#
# This is the width of the modules themselves, not of the box round them. In
# voucher.css that is .vendor-qr-box at 49.5mm outer, which is this 45mm plus a
# 2mm quiet zone and a hairline border on each side. The two numbers have to be
# changed together or this reports a size the printer never draws.
QR_DRAWN_MM = 45.0
QR_MIN_MODULE_MM = 0.50


def qr_module_mm(url: str) -> float:
    """Printed size of a single QR square, in millimetres."""
    modules = segno.make(url, error="m").symbol_size(border=0)[0]
    return QR_DRAWN_MM / modules


def qr_quality(url: str) -> dict:
    module_mm = qr_module_mm(url)
    return {
        "module_mm": round(module_mm, 2),
        "ok": module_mm >= QR_MIN_MODULE_MM,
        "url_length": len(url),
    }


def qr_svg(url: str, scale: int = 4) -> str:
    """Inline SVG for the QR code.

    Error correction M so a printed voucher still scans with a coffee ring on
    it. No quiet-zone border here: the CSS gives the code white padding.
    """
    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=scale, border=0, xmldecl=False, svgns=True, omitsize=True)
    svg = buf.getvalue().decode("utf-8")
    # Let CSS size it.
    svg = svg.replace("<svg ", '<svg class="qr-svg" preserveAspectRatio="xMidYMid meet" ', 1)
    return svg


# --------------------------------------------------------------------------
# How long a run takes
# --------------------------------------------------------------------------

def read_pace() -> dict:
    """The fixed cost and the per voucher cost of a print run, in seconds.

    Measured from real runs on this machine rather than assumed, because the
    two PDF engines are not remotely comparable and the point of the figure is
    to tell somebody watching a progress bar how far through it is.

    Both numbers are kept because the shape matters: there is a real fixed cost
    per run whatever the size, and then a cost per voucher on top. Fitted from
    the last two runs of different sizes; until there are two, the per voucher
    figure is carried and only the fixed cost is fitted.
    """
    try:
        saved = json.loads(PACE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_PACE)
    pace = dict(DEFAULT_PACE)
    for key in ("fixed_seconds", "seconds_per_voucher"):
        value = saved.get(key)
        if isinstance(value, (int, float)) and 0 <= value < 3600:
            pace[key] = float(value)
    return pace


def record_pace(vouchers: int, seconds: float) -> None:
    """Fold a finished run into the estimate.

    The shape matters: a 600 voucher sheet is not a hundred times the work of a
    6 voucher one, because a good part of a small run is the engine starting up.
    Two runs of different sizes give a line through them, and the line is what
    gets stored.

    Until there are two, one run is one point and the split between the fixed
    cost and the per voucher cost is guesswork: 30/70 is used, which is roughly
    where a real pair of measurements lands. Either way a run of the size just
    measured comes out at the time it just took, which is the part somebody
    watching the bar actually notices.
    """
    if vouchers <= 0 or seconds <= 0:
        return
    try:
        previous = json.loads(PACE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}

    last_n = previous.get("last_vouchers")
    last_s = previous.get("last_seconds")
    fitted = False
    if isinstance(last_n, int) and isinstance(last_s, (int, float))             and abs(last_n - vouchers) >= 10:
        per = (seconds - last_s) / (vouchers - last_n)
        fixed = seconds - per * vouchers
        # A negative slope or a negative fixed cost means the two runs are not
        # telling a straight story: a busy machine, or a cold cache on the
        # first. Fall back to the single point rather than storing a line that
        # runs backwards.
        fitted = per >= 0 and fixed >= 0
    if not fitted:
        fixed = seconds * 0.3
        per = seconds * 0.7 / vouchers

    # Swallowed on purpose. This is called after the vouchers are written and
    # the ledger is updated, so a read-only data folder or a full disk must not
    # be allowed to turn a finished run into the error page that says nothing
    # was written anywhere. The cost of losing this file is a progress bar that
    # guesses.
    try:
        PACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PACE_PATH.write_text(json.dumps({
            "_comment": "Written after every run so the progress bar can "
                        "estimate the next one. Delete it and the app goes back "
                        "to its starting guess. Not a setting: measured.",
            "fixed_seconds": round(fixed, 2),
            "seconds_per_voucher": round(per, 4),
            "fitted_from_two_runs": fitted,
            "last_vouchers": vouchers,
            "last_seconds": round(seconds, 2),
            "measured_at": datetime.now().isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
    except OSError:
        traceback.print_exc()


def estimate_seconds(vouchers: int, pace: dict | None = None) -> float:
    """How long a run of this size should take, at this machine's pace."""
    pace = pace or read_pace()
    return max(2.0, pace["fixed_seconds"] + pace["seconds_per_voucher"] * vouchers)


# --------------------------------------------------------------------------
# Assets
# --------------------------------------------------------------------------

def asset_exists(name: str) -> bool:
    return (ASSETS_DIR / name).is_file()


# --------------------------------------------------------------------------
# Voucher building
# --------------------------------------------------------------------------

@dataclass
class Voucher:
    # The code in the red box: the export's ID and this voucher's ticket number.
    # The only thing that says which voucher this is, since the QR code on the
    # vendor sheet is the same for every one of them.
    dmu_code: str
    value_display: str
    event_name: str
    valid_until: str
    event_date: str
    # Where this batch can be spent. Empty means fall back to the configured
    # list, which is what the specimen on the vendor sheet does.
    venues: list[str] = field(default_factory=list)

    @property
    def code_size_class(self) -> str:
        """Which type size the code takes, chosen by how long it is.

        The ID and the ticket count are DMU's to grow. Today a code is 12-01 at
        five characters; a request numbered 50000 issuing 8000 vouchers is
        50000-8000 at ten, and there is no ceiling on either. A code wider than
        its box does not wrap, it runs off the paper, so long ones step down a
        size instead.

        Buckets rather than a calculation because both PDF engines have to agree
        on the answer, and CSS that sizes text to fit its container does not
        survive WeasyPrint.
        """
        n = len(self.dmu_code)
        if n <= 13:
            return ""
        return "long" if n <= 17 else "longest"


# --------------------------------------------------------------------------
# The vendor sheet's specimen voucher and its thumbnail
# --------------------------------------------------------------------------

THUMBNAIL_PATH = APP_DIR / "static" / "sample-voucher.png"
THUMBNAIL_STAMP = APP_DIR / "static" / "sample-voucher.json"

# The picture on the vendor sheet is a PNG made once by a machine with Chromium,
# not the live artwork shrunk by CSS. WeasyPrint keeps a transformed box's full
# layout size and paginates the lower part of it away, which printed the
# thumbnail with an empty voucher-number panel on the server while Chromium drew
# it correctly. A picture renders identically in both.
#
# The cost is that the picture can go out of date, so thumbnail_fingerprint below
# covers everything that changes what a voucher looks like and the app says so
# when it no longer matches.

# Not a real date. A pre-rendered image freezes whatever date it was made with,
# and a handout printed months later would otherwise show a specimen that had
# expired, on a sheet whose own rules say to refuse an expired voucher.
SPECIMEN_VALID_UNTIL = "DD Month YYYY"

# Likewise not a real one, and the only thing standing between a printed preview
# and something that could be handed over. A code is now the export's ID and a
# ticket number, so there is no naturally unusable range the way the old random
# references gave one by starting 000. No request has ID 0, so this cannot
# collide with anything real.
SPECIMEN_DMU_CODE = "00-000"


def specimen_voucher() -> "Voucher":
    """The example voucher the vendor sheet shows and quotes.

    Fixed, and carrying the specimen code rather than a real one, so the code on
    the handout can never be one that was really issued to somebody.
    """
    return Voucher(
        dmu_code=SPECIMEN_DMU_CODE,
        value_display="£6.00",
        event_name="Example event",
        valid_until=SPECIMEN_VALID_UNTIL,
        event_date="",
        # Left empty so the specimen shows the configured list. A batch's own
        # picked venues must not reach the handout, or the fingerprint below
        # would change with every print run.
        venues=[],
    )


# The logo files the artwork can carry, in the order they are looked for. Named
# rather than "whatever is in assets", because the fingerprint below hashes them
# and the folder holds more than the app renders: the full resolution original
# DMU supplied lives there too and git does not carry it, so hashing the folder
# made the office computer and the server work out different numbers and the
# front page called the picture out of date for ever.
LOGO_FILES = {
    "dmu": ("dmu-logo.png", "dmu-logo.jpg", "dmu-logo.svg"),
}

# Everything that changes what a voucher looks like. Miss one of these out and
# the handout quietly shows a picture of the old artwork.
THUMBNAIL_INPUTS = ("static/voucher.css", "templates/_voucher.html",
                    "templates/thumbnail.html")
THUMBNAIL_CONFIG_KEYS = ("voucher_title", "reference_label", "holder_instruction",
                         "venue_intro", "venues", "venue_warning",
                         "valid_until_label", "small_print")


def _fingerprint_text(path: Path) -> bytes:
    """A tracked text file's bytes, with line endings flattened to LF.

    Git stores these files with LF and hands a Windows checkout CRLF, so the
    same tracked file is genuinely different bytes on the office computer and on
    the Linux server: voucher.css is 16529 bytes here and 15898 there. Hashing
    the bytes as they sit on disk meant a stamp written by the office computer
    could never match what the server worked out, and the front page called the
    picture out of date permanently, whoever remade it. Flattening first makes
    the two agree. The logos below are binary and are hashed as they are.
    """
    if not path.is_file():
        return b""
    return path.read_bytes().replace(b"\r\n", b"\n")


def thumbnail_fingerprint(config: dict) -> str:
    h = hashlib.sha256()
    for rel in THUMBNAIL_INPUTS:
        h.update(_fingerprint_text(APP_DIR / rel))
    h.update(json.dumps({k: config.get(k) for k in THUMBNAIL_CONFIG_KEYS},
                        sort_keys=True, ensure_ascii=False).encode("utf-8"))
    h.update(SPECIMEN_VALID_UNTIL.encode("utf-8"))
    # The logo is part of the artwork, so dropping one in makes the picture stale
    # even though no file the app owns has changed. Only the files the app would
    # actually render count: anything else in assets/ is a source file, and the
    # server does not have it.
    for names in LOGO_FILES.values():
        for name in names:
            path = ASSETS_DIR / name
            if path.is_file():
                h.update(name.encode("utf-8"))
                h.update(path.read_bytes())
    return h.hexdigest()[:16]


def thumbnail_state(config: dict) -> str:
    """'ready', 'stale' or 'missing'. 'stale' means the artwork has changed
    since the picture was made, so the handout is showing the old design."""
    if not THUMBNAIL_PATH.is_file():
        return "missing"
    try:
        with open(THUMBNAIL_STAMP, encoding="utf-8") as fh:
            stamped = json.load(fh).get("fingerprint", "")
    except (OSError, ValueError):
        return "stale"
    return "ready" if stamped == thumbnail_fingerprint(config) else "stale"


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------

def already_issued_codes(codes: list[str], ledger: list[dict] | None = None) -> list[str]:
    """Which of these codes are already in the ledger, in order, deduplicated.

    Codes are no longer drawn, they are derived from the export's ID and the
    ticket number, so the same request produces the same codes every time it is
    run. Re-issuing is allowed, but it has to be said out loud: the second set
    carries codes identical to vouchers already in circulation and no vendor
    could tell the two apart.
    """
    ledger = read_ledger() if ledger is None else ledger
    seen = {(row.get("dmu_code") or "").strip() for row in ledger}
    seen.discard("")
    return [c for c in codes if c in seen]


def format_uk_date(value: str) -> str:
    """'2026-09-30' -> '30 September 2026'. Passes anything unparseable through."""
    if not value:
        return ""
    try:
        return date.fromisoformat(value).strftime("%d %B %Y").lstrip("0")
    except ValueError:
        return value


def safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "-", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "Event"


def resolve_venues(config: dict, selected: list[str] | None,
                   extra: str = "") -> list[str]:
    """Where this batch can be spent: the ticked venues plus any one-off typed in.

    Unticking everything comes back as the configured list rather than an empty
    one. A voucher that does not say where it can be spent is useless to the
    holder and unenforceable for the vendor, so that is treated as a slip rather
    than an instruction.

    The one-off is deliberately not written back to config.json. It belongs to
    this print run, and the specimen on the vendor sheet has to go on showing the
    settled list or its fingerprint would move every time somebody typed one in.
    """
    known = [v for v in (config.get("venues") or []) if str(v).strip()]
    picked = [v for v in known if v in set(selected or [])]
    extra = re.sub(r"\s+", " ", extra or "").strip()
    if extra and extra not in picked:
        picked.append(extra)
    return picked or known


def build_vouchers(request: VoucherRequest, venues: list[str] | None = None,
                   specimen: bool = False) -> list[Voucher]:
    """The printable vouchers for one request.

    Nothing is drawn or reserved here: a code is the export's ID and the ticket
    number, so it is a property of the request rather than something allocated.
    That is why this no longer takes a list of references.

    `specimen` prints SPECIMEN_DMU_CODE on every voucher instead of the real
    code, and previews use it. It matters more than it looks: a preview does get
    printed by accident occasionally, and it must be impossible for that sheet to
    carry a code somebody could hand over. The random references this replaced
    gave that away for free by starting 000, and nothing about ID-and-ticket
    does.

    Both dates come off the request, because they come off the row in the
    export. There is no way to pass a different one in, which is the point: one
    date typed on the form used to be applied to every event in a mixed drop.
    """
    return [
        Voucher(
            dmu_code=SPECIMEN_DMU_CODE if specimen else request.dmu_code_for(ticket),
            value_display=request.value_display,
            event_name=request.event_name,
            valid_until=format_uk_date(request.expiry_date),
            event_date=format_uk_date(request.event_date),
            venues=list(venues or []),
        )
        for ticket in range(1, request.count + 1)
    ]


def chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------
# PDF output via Chromium
# --------------------------------------------------------------------------

class PdfWriter:
    """Renders HTML to A4 PDF, with whichever engine this machine has.

    Two engines, one set of templates. The rendered HTML is self-contained: the
    stylesheet is inlined, the logos are data URIs and the QR code is inline
    SVG. So an engine has nothing to fetch and swapping engines cannot break
    asset loading, only layout.

    Chromium, through Playwright, where it is installed. On the office computer
    that means the on-screen preview and the printed PDF come from the same
    engine, which is the strongest guarantee of a faithful print.

    WeasyPrint where Chromium is not, which means a server. Chromium is 427 MB
    and a free PythonAnywhere account has 512 MB in total, so a browser is not
    an option there. WeasyPrint is pure Python and reads the same HTML and CSS.

    Set DMU_PDF_ENGINE to "chromium" or "weasyprint" to force one, which is how
    you compare the two on a machine that has both.
    """

    def __init__(self, engine: str = "") -> None:
        self.engine = (engine or os.environ.get("DMU_PDF_ENGINE", "")).strip().lower()
        self._playwright = None
        self._browser = None
        self._weasy = None
        self.warnings: list[str] = []

    def __enter__(self) -> "PdfWriter":
        wanted = self.engine

        if wanted in ("", "chromium"):
            try:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch()
                self.engine = "chromium"
                return self
            except Exception:
                # Tidy up a half-started Playwright before falling back, or it
                # leaves a node process behind.
                if self._playwright:
                    try:
                        self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                if wanted == "chromium":
                    raise

        import weasyprint
        self._weasy = weasyprint
        self.engine = "weasyprint"

        # WeasyPrint reports unsupported CSS to a logger rather than raising.
        # Collected so a caller can say what it could not draw, instead of
        # quietly producing a voucher that is subtly wrong.
        import logging

        class _Collect(logging.Handler):
            def __init__(self, sink: list[str]) -> None:
                super().__init__()
                self.sink = sink

            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.WARNING:
                    self.sink.append(record.getMessage())

        self._log_handler = _Collect(self.warnings)
        logging.getLogger("weasyprint").addHandler(self._log_handler)
        return self

    def __exit__(self, *exc) -> None:
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        if self._weasy is not None:
            import logging
            logging.getLogger("weasyprint").removeHandler(self._log_handler)

    def write(self, html: str, out_path: Path, base_url: Path = APP_DIR) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if self.engine == "chromium":
            self._write_chromium(html, out_path, base_url)
        else:
            self._write_weasyprint(html, out_path, base_url)

    def _write_chromium(self, html: str, out_path: Path, base_url: Path) -> None:
        page = self._browser.new_page()
        try:
            # A file:// base URL so <img src="assets/..."> resolves.
            page.goto(base_url.as_uri() + "/")
            page.set_content(html, wait_until="load")
            page.pdf(
                path=str(out_path),
                format="A4",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                prefer_css_page_size=True,
            )
        finally:
            page.close()

    def _write_weasyprint(self, html: str, out_path: Path, base_url: Path) -> None:
        # No format or margin arguments: the stylesheet's @page rule already
        # says A4 portrait with no margin, and WeasyPrint honours it.
        self._weasy.HTML(string=html, base_url=base_url.as_uri() + "/").write_pdf(
            str(out_path))


def pdf_engines_available() -> dict[str, str]:
    """Which engines this machine can actually use, and why not if it cannot."""
    out = {}
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch()
            browser.close()
            out["chromium"] = "ready"
        finally:
            pw.stop()
    except Exception as exc:
        out["chromium"] = f"not available: {type(exc).__name__}"
    try:
        import weasyprint
        out["weasyprint"] = f"ready, version {weasyprint.__version__}"
    except Exception as exc:
        out["weasyprint"] = f"not available: {type(exc).__name__}"
    return out


def unique_output_dir(event_name: str, when: datetime, dmu_id: str = "") -> Path:
    """One folder per request, named so it can be handed straight on.

    The ID is in the name because the folder is the unit that goes to whoever
    asked for the vouchers: one request, one folder, one zip, and the codes
    inside it all begin with that same ID. "ID 12" is what the requestor will
    quote back, so it should be what they see on what they are sent.
    """
    tag = f"ID {safe_folder_name(dmu_id)} " if dmu_id else ""
    base = OUTPUT_DIR / f"{when:%Y-%m-%d} {tag}{safe_folder_name(event_name)}"
    candidate = base
    n = 2
    while candidate.exists():
        candidate = Path(f"{base} ({n})")
        n += 1
    return candidate


def write_batch_summary(path: Path, request: VoucherRequest, vouchers: list[Voucher],
                        batch: int, issued_by: str, issued_at: datetime,
                        venues: list[str] | None = None) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Event", request.event_name])
        writer.writerow(["DMU ID", request.dmu_id])
        writer.writerow(["Cost centre", request.cost_centre])
        writer.writerow(["Budget approver", request.budget_approver])
        writer.writerow(["Lead contact", request.lead_contact])
        writer.writerow(["Vouchers", request.count])
        writer.writerow(["Value per voucher", request.value_display])
        writer.writerow(["Total value", request.total_display])
        writer.writerow(["Event date", format_uk_date(request.event_date)])
        writer.writerow(["Valid until", format_uk_date(request.expiry_date)])
        writer.writerow(["Redeemable at", ", ".join(venues or [])])
        writer.writerow(["Batch", f"{batch:03d}"])
        writer.writerow(["Issued", issued_at.strftime("%d %B %Y %H:%M")])
        writer.writerow(["Issued by", issued_by])
        writer.writerow([])
        writer.writerow(["Voucher code", "Value", "Redeemed at", "Date redeemed"])
        for v in vouchers:
            writer.writerow([v.dmu_code, v.value_display, "", ""])


def copy_source_csv(source: Path | None, dest_dir: Path) -> None:
    if source and source.is_file():
        try:
            shutil.copy2(source, dest_dir / f"Source export - {source.name}")
        except OSError:
            pass


def open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606
        elif os.uname().sysname == "Darwin":
            os.system(f'open "{path}"')
    except Exception:
        pass
