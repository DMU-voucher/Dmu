"""Settings for the redemption site.

Everything here can be overridden by an environment variable, which is how the
passwords get set on PythonAnywhere without ever being written into a file that
goes to GitHub. Set them in the web app's Environment variables section, or in
a .env file next to this one, which is gitignored.

    DMU_VENDOR_PASSWORD   what the vendors are told at the venue
    DMU_ADMIN_PASSWORD    what DMU Venues uses to import batches and reconcile
    DMU_SECRET_KEY        signs the session cookie
    DMU_DB_PATH           where the database lives, if not the default
    DMU_VENUES            venue names, separated by |
    DMU_SITE_PASSWORD     optional gate across the whole site
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Read a .env file next to this one, if there is one.

    Deliberately not a dependency. Four lines of parsing beats asking whoever
    sets this up on PythonAnywhere to pip install something first.
    """
    path = HERE / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DB_PATH = Path(os.environ.get("DMU_DB_PATH") or (HERE / "data" / "redemptions.db"))

VENDOR_PASSWORD = os.environ.get("DMU_VENDOR_PASSWORD", "")
ADMIN_PASSWORD = os.environ.get("DMU_ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("DMU_SECRET_KEY", "")

VENUES = [v.strip() for v in os.environ.get(
    "DMU_VENUES", "Saints of Mokha|Devorer Street Vendor (outside Monty's)"
).split("|") if v.strip()]

# An optional gate across the whole site, in front of even the sign-in page.
# Leave it unset in normal running: vendors must not need two passwords.
#
# Set it while the site is being tested on a public address. Without it the
# sign-in page is reachable by anyone who finds the URL, which during testing
# means a DMU-branded login screen sitting open on the internet with test data
# behind it. With it set, there is nothing to see at all.
SITE_USER = os.environ.get("DMU_SITE_USER", "dmu")
SITE_PASSWORD = os.environ.get("DMU_SITE_PASSWORD", "")


# A vendor signs in at the start of a shift, not per customer.
VENDOR_SESSION_HOURS = int(os.environ.get("DMU_VENDOR_SESSION_HOURS", "16"))

# Wrong numbers happen. Guessing at numbers should not be cheap. Counted per
# signed-in venue, over a rolling window.
MAX_FAILURES = int(os.environ.get("DMU_MAX_FAILURES", "12"))
FAILURE_WINDOW_MINUTES = int(os.environ.get("DMU_FAILURE_WINDOW_MINUTES", "10"))


def configured() -> list[str]:
    """What still needs setting before the site should be let anywhere near a
    vendor. Shown on the page rather than failing quietly."""
    missing = []
    if not VENDOR_PASSWORD:
        missing.append("DMU_VENDOR_PASSWORD")
    if not ADMIN_PASSWORD:
        missing.append("DMU_ADMIN_PASSWORD")
    if not SECRET_KEY:
        missing.append("DMU_SECRET_KEY")
    return missing
