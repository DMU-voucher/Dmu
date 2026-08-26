"""Make the picture of a voucher that goes on the vendor instruction sheet.

    python make_sample_thumbnail.py

Run this on a machine with Chromium, which means the office computer. It writes
static/sample-voucher.png and a fingerprint beside it, and both belong in git so
the server has them: the server renders with WeasyPrint and cannot make this
file itself.

Run it again whenever you change how a voucher looks, which means voucher.css,
_voucher.html, the wording in config.json, or the logos in assets. You do not
have to remember: the front page of the app says when the picture no longer
matches the artwork, and so does check_pdf_engine.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import vouchers as core

# 3x so the picture is around 550 dpi where it lands on the page at 34.5mm, well
# clear of what any office printer resolves. A voucher is mostly white, so the
# PNG stays small.
SCALE = 3


def main() -> int:
    config = core.load_config()

    import app as generator
    with generator.app.test_request_context():
        html = generator.render_thumbnail_page(config)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print()
        print("  Playwright is not installed, so there is no Chromium to take the")
        print("  picture with. Run this on the office computer instead.")
        print()
        return 1

    core.THUMBNAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(device_scale_factor=SCALE)
            page.goto(core.APP_DIR.as_uri() + "/")
            page.set_content(html, wait_until="load")
            page.locator(".voucher").screenshot(path=str(core.THUMBNAIL_PATH))
        finally:
            browser.close()

    fingerprint = core.thumbnail_fingerprint(config)
    core.THUMBNAIL_STAMP.write_text(json.dumps({
        "_comment": "Written by make_sample_thumbnail.py. If the fingerprint "
                    "here stops matching the artwork, the app says the picture "
                    "is out of date. Do not edit by hand.",
        "fingerprint": fingerprint,
    }, indent=2) + "\n", encoding="utf-8")

    size_kb = core.THUMBNAIL_PATH.stat().st_size / 1024
    print()
    print(f"  Wrote {core.THUMBNAIL_PATH.name} ({size_kb:.0f} KB) and its fingerprint.")
    print(f"  Fingerprint {fingerprint}. State: {core.thumbnail_state(config)}.")
    print("  Commit both files, or the server will carry the old picture.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
