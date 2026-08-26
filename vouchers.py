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
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path

import segno

import refs

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

# The loose copy of the vendor sheet, refreshed after every run so the current
# one is always to hand without digging through Output.
LOOSE_VENDOR_PDF = DATA_DIR / "Vendor instructions.pdf"

PLACEHOLDER_QR_URL = "PASTE THE MICROSOFT FORM ADDRESS HERE"

LEDGER_FIELDS = [
    "voucher_code",
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


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------

HEADER_ALIASES = {
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
    warnings: list[str] = field(default_factory=list)

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

    def as_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["value_display"] = self.value_display
        d["total_display"] = self.total_display
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
                "codes": [r.get("voucher_code", "") for r in batch_rows],
                "output_folder": batch_rows[0].get("output_folder", ""),
            }
    return None


def append_ledger(rows: list[dict]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LEDGER_PATH.exists()
    with open(LEDGER_PATH, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LEDGER_FIELDS)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in LEDGER_FIELDS})


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
# Assets
# --------------------------------------------------------------------------

def asset_exists(name: str) -> bool:
    return (ASSETS_DIR / name).is_file()


# --------------------------------------------------------------------------
# Voucher building
# --------------------------------------------------------------------------

@dataclass
class Voucher:
    code: str
    value_display: str
    event_name: str
    valid_until: str
    event_date: str


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


def specimen_voucher() -> "Voucher":
    """The example voucher the vendor sheet shows and quotes.

    Fixed, and drawn from the same low numbers as any other preview, so the
    number on the handout can never be one that was really issued to somebody.
    """
    return Voucher(
        code=sample_references(1)[0],
        value_display="£6.00",
        event_name="Example event",
        valid_until=SPECIMEN_VALID_UNTIL,
        event_date="",
    )


# Everything that changes what a voucher looks like. Miss one of these out and
# the handout quietly shows a picture of the old artwork.
THUMBNAIL_INPUTS = ("static/voucher.css", "templates/_voucher.html",
                    "templates/thumbnail.html")
THUMBNAIL_CONFIG_KEYS = ("voucher_title", "reference_label", "holder_instruction",
                         "venue_intro", "venues", "venue_warning",
                         "valid_until_label", "small_print")


def thumbnail_fingerprint(config: dict) -> str:
    h = hashlib.sha256()
    for rel in THUMBNAIL_INPUTS:
        path = APP_DIR / rel
        h.update(path.read_bytes() if path.is_file() else b"")
    h.update(json.dumps({k: config.get(k) for k in THUMBNAIL_CONFIG_KEYS},
                        sort_keys=True, ensure_ascii=False).encode("utf-8"))
    h.update(SPECIMEN_VALID_UNTIL.encode("utf-8"))
    # The logos are part of the artwork, so dropping one in makes the picture
    # stale even though no file the app owns has changed.
    for name in sorted(p.name for p in ASSETS_DIR.glob("*")
                       if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}):
        h.update(name.encode("utf-8"))
        h.update((ASSETS_DIR / name).read_bytes())
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

def draw_references(count: int) -> list[str]:
    """Numbers for a batch about to be printed.

    The ledger is the record of everything that has gone out, so the ledger is
    what a new number is checked against. There is no pool and no database: a
    number exists once it is written to the ledger, and not before.
    """
    return refs.draw_references(count, {r["voucher_code"] for r in read_ledger()})


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


def build_vouchers(request: VoucherRequest, references: list[str],
                   valid_until: str, event_date: str) -> list[Voucher]:
    """Dress a list of references as printable vouchers.

    Drawing the numbers is separate on purpose. A preview must never write a
    real number to the ledger, so it passes in throwaway ones.
    """
    return [
        Voucher(
            code=reference,
            value_display=request.value_display,
            event_name=request.event_name,
            valid_until=format_uk_date(valid_until),
            event_date=format_uk_date(event_date),
        )
        for reference in references
    ]


def sample_references(count: int) -> list[str]:
    """References for a preview or the vendor handout. Never recorded anywhere.

    Real references are drawn from 10,000,000 upwards, so these low numbers
    print with leading zeros and can never collide with a live voucher. That
    matters: a preview does get printed by mistake occasionally, and it must be
    impossible for that sheet to carry somebody else's number.
    """
    return [refs.make_reference(i) for i in range(1, count + 1)]


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


def unique_output_dir(event_name: str, when: datetime) -> Path:
    base = OUTPUT_DIR / f"{when:%Y-%m-%d} {safe_folder_name(event_name)}"
    candidate = base
    n = 2
    while candidate.exists():
        candidate = Path(f"{base} ({n})")
        n += 1
    return candidate


def write_batch_summary(path: Path, request: VoucherRequest, vouchers: list[Voucher],
                        batch: int, valid_until: str, event_date: str, issued_by: str,
                        issued_at: datetime) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Event", request.event_name])
        writer.writerow(["Cost centre", request.cost_centre])
        writer.writerow(["Budget approver", request.budget_approver])
        writer.writerow(["Lead contact", request.lead_contact])
        writer.writerow(["Vouchers", request.count])
        writer.writerow(["Value per voucher", request.value_display])
        writer.writerow(["Total value", request.total_display])
        writer.writerow(["Event date", format_uk_date(event_date)])
        writer.writerow(["Valid until", format_uk_date(valid_until)])
        writer.writerow(["Batch", f"{batch:03d}"])
        writer.writerow(["Issued", issued_at.strftime("%d %B %Y %H:%M")])
        writer.writerow(["Issued by", issued_by])
        writer.writerow([])
        writer.writerow(["Voucher number", "Value", "Redeemed at", "Date redeemed"])
        for v in vouchers:
            writer.writerow([v.code, v.value_display, "", ""])


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
