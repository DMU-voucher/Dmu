"""What PythonAnywhere loads to serve the voucher generator.

In the Web tab, open the WSGI configuration file it made for you, delete what is
in it, and put these two lines there instead:

    import sys; sys.path.insert(0, "/home/YOURNAME/dmu-vouchers")
    from wsgi import application

Then press Reload. Nothing takes effect until you do.

The passwords and paths come from a `.env` file next to this one, which is
gitignored and never leaves the server. There is no Environment variables
section in PythonAnywhere's Web tab, whatever older notes in this repository
said, so this file and that one are the whole of the configuration.

Everything below has to be set before `app` is imported: the data folder is read
when `vouchers` loads and the password when `app` loads, so the import stays at
the bottom of this file.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def load_env_file() -> None:
    """Read `.env` next to this file, if there is one.

    Deliberately not a dependency: a dozen lines beats asking whoever sets this
    up to pip install something first. A real environment variable wins over the
    file, so anything set for the whole account is not silently overridden here.
    """
    path = HERE / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

# Served on a public address, not started from run.bat. The pages drop the
# "nothing is uploaded" wording, offer a zip download instead of opening a
# folder, and refuse to serve anything at all if the password is missing.
os.environ["DMU_HOSTED"] = "1"

# The finished batches and the uploaded CSVs. Outside the repository folder, or
# deploying a new version takes the finished batches with it. Override in .env
# if the account layout differs.
os.environ.setdefault("DMU_DATA_DIR", str(Path.home() / "dmu-voucher-data"))

# Chromium is 427 MB against a free account's 512 MB, so a server draws PDFs
# with WeasyPrint. Forced rather than left to fall back, so a half-installed
# Playwright fails at startup where you can see it, rather than on the first
# batch somebody tries to print.
os.environ.setdefault("DMU_PDF_ENGINE", "weasyprint")

from app import app as application  # noqa: E402
