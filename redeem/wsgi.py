"""What PythonAnywhere loads to serve the site.

In the PythonAnywhere Web tab, set the WSGI configuration file to point here,
or paste these two lines into the one it made for you:

    import sys; sys.path.insert(0, "/home/YOURNAME/dmu-catering/redeem")
    from wsgi import application

Set the passwords under Environment variables on the same page rather than in a
file, and set DMU_DB_PATH to somewhere outside the repository folder so that
pulling a new version cannot take the redemptions with it.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# This folder first. The generator alongside it also has an app.py, and getting
# these the wrong way round loads that one instead, which serves no routes.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(HERE.parent) not in sys.path:
    sys.path.append(str(HERE.parent))

from app import app as application  # noqa: E402,F401
