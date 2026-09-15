"""Make a print run and check the app actually made it, end to end.

Run this anywhere the app is installed, with the pip that matches the Python the
web app uses:

    python3.10 check_runs.py

It exits non-zero if anything is wrong, so it can be treated as a test. Nothing
it does touches the real records: it makes up its own approval export and writes
everything to a throwaway folder in the system temp directory.

**What it is guarding.** In September 2026 a run of a whole export came back as
PythonAnywhere's own "Something went wrong :-(" page. The run was one request,
the export was 1,090 vouchers across five events, and the load balancer in front
of the app hangs up on anything still going after five minutes. The fix was to
draw a run a few pages at a time and join the pieces back together, which is a
lot more machinery than one request was, and machinery is what quietly stops
working. So each thing that fix relies on is checked here by doing it:

  - a run comes out with every voucher, once, in order, in one sheet per event
  - no single request draws the whole run
  - a request that is asked for twice draws once
  - a run finishes even if the process running it is replaced halfway
  - a machine that cannot rejoin a sheet draws each event whole instead
  - a run that stops partway says how much of it is real, and it is right
  - a run nobody finishes leaves no empty batch folder behind
  - a sheet that came out the wrong length stops the run instead of printing
  - the issued stamp still names its timezone
  - a browser with no JavaScript can still make vouchers
  - no request answers with a server error, whatever goes wrong inside it

**Run it on the server too.** Chromium is 427 MB and will not fit on a free
PythonAnywhere account, so the server draws with WeasyPrint. This check is
engine-agnostic and passing here proves nothing about there. See
check_pdf_engine.py, which is about the artwork rather than the machinery.
"""

from __future__ import annotations

import builtins
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
SCRATCH_PREFIX = "dmu-check-"


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def scratch_folder() -> Path:
    """Where this check is allowed to write, which is never the app's own data.

    Guarded rather than assumed, and the guard is not decoration. An earlier
    version of this file worked out the folder as Path(os.environ.get(...) or
    ""), which is Path("."), which is truthy, so the temp folder was never made
    and the check ran against the real records and then deleted the app folder
    it was sitting in. Everything below is written so that mistake cannot be
    made again: the folder has to be under the system temp directory and named
    by this script, or nothing runs and nothing is deleted.
    """
    given = os.environ.get("DMU_CHECK_DIR", "").strip()
    path = (Path(given).resolve() if given
            else Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX)).resolve())
    if not disposable(path):
        raise SystemExit(
            f"  check_runs.py will not run against {path}.\n"
            f"  It writes and deletes freely, so it only works in a folder it\n"
            f"  made itself: one under {TEMP_ROOT} named {SCRATCH_PREFIX}...\n"
            f"  Unset DMU_CHECK_DIR and it will make one.")
    return path


def disposable(path: Path) -> bool:
    """Whether this script may delete the folder and everything under it."""
    return (_inside(path, TEMP_ROOT)
            and path.name.startswith(SCRATCH_PREFIX)
            and not _inside(HERE, path))


SCRATCH = scratch_folder()
os.environ["DMU_CHECK_DIR"] = str(SCRATCH)
# Set before the app is imported: vouchers.py reads it as it loads, and the
# whole point is that this check cannot write into the real Output folder.
os.environ["DMU_DATA_DIR"] = str(SCRATCH)

sys.path.insert(0, str(HERE))

import app as webapp  # noqa: E402
import vouchers as core  # noqa: E402

if not _inside(core.DATA_DIR.resolve(), SCRATCH):
    raise SystemExit(f"  the app is writing to {core.DATA_DIR}, not {SCRATCH}; "
                     "refusing to run")

# Obviously invented. A check that quotes a real event name in its output is a
# check nobody can paste into a ticket.
#
# --quick makes the events small. Everything below is still exercised: the run
# is still sliced, still rejoined, still picked up by another process. It is for
# the server, where drawing is the whole cost. A free PythonAnywhere account has
# a daily CPU allowance of about a hundred seconds, and running out of it does
# not stop the site, it slows everything down for the rest of the day. The full
# size below draws over fourteen hundred vouchers across all the checks, which
# WeasyPrint will not do inside that. --quick is roughly a quarter of it, and
# even that is worth spending deliberately rather than by accident.
QUICK = "--quick" in sys.argv
EVENTS = ([("901", "Check Event A", 30), ("902", "Check Event B", 6)] if QUICK
          else [("901", "Check Event A", 90), ("902", "Check Event B", 40)])
TOTAL = sum(count for _, _, count in EVENTS)

HEADER = ('"ID","Event Name ","Number of Vouchers","Value Per Voucher",'
          '"Total Value","Cost Centre","Status","Budget Approver",'
          '"Lead Contact","Approval Code","Expiry Date","Event Date"')


def export_csv() -> bytes:
    rows = [HEADER]
    for dmu_id, name, count in EVENTS:
        rows.append(f'"{dmu_id}","{name}","{count}","£5.00",'
                    f'"£{count * 5}.00","10001000","Approved",,,'
                    f'"check-{dmu_id}","31/12/2026",')
    return ("\n".join(rows) + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# Talking to the app
# --------------------------------------------------------------------------

def upload(client) -> str:
    """Drop the export in and come back with the token the page would carry."""
    page = client.post("/upload", content_type="multipart/form-data", data={
        "csv": (io.BytesIO(export_csv()), "check-export.csv")})
    found = re.search(r'name="token" value="([^"]+)"', page.get_data(as_text=True))
    if not found:
        raise AssertionError("the upload page came back with no token")
    return found.group(1)


def plan(client, token: str, selected=(0, 1)) -> dict:
    reply = client.post("/generate/plan", data={
        "token": token, "selected": [str(i) for i in selected],
        "issued_by": "check_runs.py", "venues": ["Saints of Mokha"]})
    assert_status(reply, "/generate/plan")
    body = reply.get_json()
    if "state" not in body:
        raise AssertionError(f"a run would not start: {body}")
    return body["state"]


def step(client, state: dict) -> dict:
    reply = client.post("/generate/step", data={
        "job": state["job"], "after": str(state["steps"])})
    assert_status(reply, "/generate/step")
    return reply.get_json()


def run_to_the_end(client, state: dict, limit: int = 400) -> tuple[dict, list[int]]:
    """Every slice, one after the other, the way the page asks for them."""
    sizes = []
    while not state["finished"]:
        if len(sizes) >= limit:
            raise AssertionError("the run never finished; it is going round")
        before = state["done"]
        body = step(client, state)
        if body.get("error") or body.get("failed"):
            raise AssertionError(f"a slice failed: {body}")
        state = body["state"]
        sizes.append(state["done"] - before)
    return state, sizes


def assert_status(reply, what: str) -> None:
    """No request may answer with a server error.

    This is the whole of the original fault on the browser's side. The page
    sends these with fetch, which treats a 504 as a perfectly good answer, so
    whatever comes back gets drawn: a run that ran out of time drew the hosting
    company's error page in place of the app. The page checks now, and so does
    this. Nothing here may hand it something it should not draw.
    """
    if reply.status_code >= 500:
        raise AssertionError(f"{what} answered {reply.status_code}, "
                             "which the page would have to draw")


# --------------------------------------------------------------------------
# Reading back what was written
# --------------------------------------------------------------------------

def sheets() -> dict[str, Path]:
    if not core.OUTPUT_DIR.exists():
        return {}
    return {folder.name: next(folder.glob("Print sheet*.pdf"))
            for folder in sorted(core.OUTPUT_DIR.glob("*"))
            if folder.is_dir() and any(folder.glob("Print sheet*.pdf"))}


def codes_in(pdf: Path) -> list[str]:
    import pymupdf  # noqa: PLC0415
    with pymupdf.open(str(pdf)) as doc:
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
    # One to three digits after the dash: a code is padded to the width of its
    # own event's count, so an event of 6 prints 902-1 and one of 600 prints
    # 901-001. Matching only the wide form made --quick read every sheet as
    # empty, which looked exactly like the app losing them.
    return re.findall(r"\b90\d-\d{1,3}\b", text)


def pages_in(pdf: Path) -> int:
    import pymupdf  # noqa: PLC0415
    with pymupdf.open(str(pdf)) as doc:
        return doc.page_count


def clear_output() -> None:
    """Start the next check from nothing.

    Both of these are under the throwaway folder, which scratch_folder() has
    already refused to let be anywhere but a temp directory of this script's
    own making. Checked again here rather than trusted, because this is the
    line that deletes things.
    """
    for folder in (core.OUTPUT_DIR, webapp.JOBS_DIR):
        if not _inside(folder.resolve(), SCRATCH):
            raise SystemExit(f"  refusing to clear {folder}: it is not in {SCRATCH}")
        shutil.rmtree(folder, ignore_errors=True)


def every_voucher_is_there(where: str) -> list[str]:
    """One sheet per event, holding that event's vouchers once each, in order."""
    problems = []
    made = sheets()
    if len(made) != len(EVENTS):
        return [f"{where}: {len(made)} sheets, expected {len(EVENTS)}"]
    per_page = int(core.load_config().get("vouchers_per_page") or 6)
    for dmu_id, name, count in EVENTS:
        folder = next((k for k in made if f"ID {dmu_id} " in k), None)
        if folder is None:
            problems.append(f"{where}: no folder for {name}")
            continue
        found = codes_in(made[folder])
        wanted = [f"{dmu_id}-{n:0{len(str(count))}d}" for n in range(1, count + 1)]
        if found != wanted:
            problems.append(
                f"{where}: {name} has {len(found)} codes ({len(set(found))} of "
                f"them different), expected {count} in order")
        pages = pages_in(made[folder])
        if pages != -(-count // per_page):
            problems.append(f"{where}: {name} is {pages} pages, "
                            f"expected {-(-count // per_page)}")
    return problems


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------

def check_a_whole_run(client) -> list[str]:
    """A run comes out complete, and comes out in pieces.

    The slices are forced to the smallest they can be, so the sheets really are
    rejoined from a dozen pieces rather than one or two. A merge that dropped a
    piece or put one back in the wrong place shows up here as missing or
    out-of-order codes.
    """
    clear_output()
    was = webapp.STEP_TARGET_SECONDS
    webapp.STEP_TARGET_SECONDS = 0.001
    try:
        state, sizes = run_to_the_end(client, plan(client, upload(client)))
    finally:
        webapp.STEP_TARGET_SECONDS = was

    problems = every_voucher_is_there("a whole run")
    if state["done"] != TOTAL:
        problems.append(f"a whole run: drew {state['done']} of {TOTAL}")
    if len(sizes) < 4:
        problems.append(f"a whole run: {len(sizes)} request(s) drew the lot; "
                        "the point is that no one request has to")
    done = client.post("/generate/done", data={"job": state["job"]})
    assert_status(done, "/generate/done")
    if done.get_data(as_text=True).count("Print sheet") != len(EVENTS):
        problems.append("a whole run: the finished page does not list every sheet")
    return problems


def check_no_request_draws_everything(client) -> list[str]:
    """Left to size itself, a run still asks for more than one slice.

    The first slice of a run is capped whatever the saved pace claims, so a
    machine that has never been measured cannot be talked into drawing a
    thousand vouchers in one request.
    """
    clear_output()
    state, sizes = run_to_the_end(client, plan(client, upload(client)))
    problems = every_voucher_is_there("sizing itself")
    if not sizes:
        return problems + ["sizing itself: no slices at all"]
    per_page = int(core.load_config().get("vouchers_per_page") or 6)
    if sizes[0] > webapp.FIRST_STEP_MAX_PAGES * per_page:
        problems.append(
            f"sizing itself: the first slice was {sizes[0]} vouchers, over the "
            f"{webapp.FIRST_STEP_MAX_PAGES}-page cap that makes it safe on a "
            "machine nothing has been measured on")
    if max(sizes) > webapp.MAX_STEP_PAGES * per_page:
        problems.append(f"sizing itself: a slice of {max(sizes)} vouchers is "
                        f"over the {webapp.MAX_STEP_PAGES}-page ceiling")
    return problems


def check_asking_twice_draws_once(client) -> list[str]:
    """A slice whose answer went missing is safe to ask for again.

    This is what lets the page retry. Without it a retry would draw the same
    pages a second time and the sheet would come out long.
    """
    clear_output()
    state = plan(client, upload(client))
    first = step(client, state)["state"]
    again = client.post("/generate/step", data={"job": state["job"], "after": "0"})
    assert_status(again, "/generate/step")
    repeated = again.get_json()["state"]
    problems = []
    if repeated != first:
        problems.append(f"asking twice: the run moved on a repeat; "
                        f"{first['done']} became {repeated['done']}")
    run_to_the_end(client, repeated)
    return problems + every_voucher_is_there("asking twice")


def check_it_survives_a_restart(client) -> list[str]:
    """A run finishes even though the process that started it is gone.

    PythonAnywhere recycles a worker whenever it likes and a free account has
    one, so a run of any length will meet this. Everything a run needs is on
    disk; the second half of this one is finished by a genuinely new Python.
    """
    clear_output()
    state = step(client, plan(client, upload(client)))["state"]
    if state["finished"]:
        return ["surviving a restart: the run finished in one slice, "
                "so nothing was carried across"]
    finished = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--finish", state["job"]]
        + (["--quick"] if QUICK else []),
        capture_output=True, text=True, cwd=str(HERE), env=dict(os.environ))
    if finished.returncode != 0:
        return ["surviving a restart: the new process could not finish the run: "
                + (finished.stderr or finished.stdout).strip()[-300:]]
    return every_voucher_is_there("surviving a restart")


def check_a_run_that_stops(client) -> list[str]:
    """A run that stops partway tells the truth about what it got through.

    The old single-request handler said nothing was written anywhere, which a
    sliced run can make a lie: the events before the one that failed are
    finished and their folders complete. Somebody reprinting on the strength of
    that message would make every voucher in them twice.
    """
    clear_output()
    state = plan(client, upload(client))
    drawn = core.PdfWriter.write

    def stop_on_the_second_event(self, html, out_path, base_url=core.APP_DIR):
        if re.search(r"[\\/]001-", str(out_path)):
            raise RuntimeError("check_runs.py stopping the run on purpose")
        return drawn(self, html, out_path, base_url)

    core.PdfWriter.write = stop_on_the_second_event
    try:
        body = {"state": state}
        while not body["state"]["finished"]:
            body = step(client, body["state"])
            if body.get("failed"):
                break
        else:
            return ["a run that stops: it did not stop"]
    finally:
        core.PdfWriter.write = drawn

    problems = []
    said, made = body.get("events_done"), sheets()
    if said != len(made):
        problems.append(f"a run that stops: it said {said} event(s) were "
                        f"finished; {len(made)} sheet(s) exist")
    if said in (None, 0, len(EVENTS)):
        problems.append(f"a run that stops: {said} finished is not a partial run")
    for name, pdf in made.items():
        found = re.search(r"ID (\d+) ", name)
        if found:
            count = next(c for i, _, c in EVENTS if i == found.group(1))
            if len(codes_in(pdf)) != count:
                problems.append(f"a run that stops: {name} is not complete, "
                                "so counting it as finished is wrong")
    return problems


def check_no_empty_folders(client) -> list[str]:
    """A run nobody finishes leaves no batch folder behind.

    The folder is made when the event finishes rather than when it starts. An
    empty one named exactly like a real batch is a trap twice over: it reads as
    a batch, and the next attempt writes "(2)" beside it.
    """
    clear_output()
    step(client, plan(client, upload(client)))  # one slice, then walk away
    if not core.OUTPUT_DIR.exists():
        return []
    empty = [f.name for f in core.OUTPUT_DIR.glob("*")
             if f.is_dir() and not any(f.iterdir())]
    return [f"walking away: empty batch folder(s) left behind: {empty}"] if empty else []


def check_a_short_sheet_stops_the_run(client) -> list[str]:
    """A sheet that came out the wrong length must not go to print.

    The slices are separate files written by separate requests, so a lost one is
    the failure this cannot be allowed to have: a sheet quietly missing a page
    looks fine until somebody is short of vouchers at a till.
    """
    clear_output()
    state = plan(client, upload(client))
    real_merge = core.merge_pdfs
    core.merge_pdfs = lambda parts, out_path: (real_merge(parts, out_path), 1)[1]
    try:
        body = {"state": state}
        for _ in range(400):
            body = step(client, body["state"])
            if body.get("failed") or body["state"]["finished"]:
                break
    finally:
        core.merge_pdfs = real_merge
    if not body.get("failed"):
        return ["a short sheet: the run finished with a sheet one page long"]
    return []


def check_the_stamp_names_its_zone(client) -> list[str]:
    """The Issued row still says BST or GMT, not UTC+01:00.

    A run is spread over several requests now, so the moment it started is
    written down and read back rather than held in memory, and a plain
    fromisoformat rebuilds the offset without the zone. That row exists to say
    which clock the time is on, and the server runs on UTC.
    """
    clear_output()
    run_to_the_end(client, plan(client, upload(client)))
    problems = []
    for folder in sorted(core.OUTPUT_DIR.glob("*")):
        summary = next(folder.glob("Batch summary*.csv"), None)
        if summary is None:
            continue
        with open(summary, encoding="utf-8-sig") as fh:
            issued = next((r[1] for r in csv.reader(fh) if r and r[0] == "Issued"), "")
        if not issued:
            problems.append(f"the stamp: {folder.name} has no Issued row")
        elif re.search(r"(UTC|GMT)[+-]\d", issued):
            problems.append(f"the stamp: {folder.name} says '{issued}', which "
                            "names an offset rather than a zone")
    return problems


def check_no_javascript(client) -> list[str]:
    """A browser that cannot drive the slices itself can still make vouchers.

    One request for the whole run, which is what it was before, and on the
    server still at the mercy of the five minute limit. It has to work, because
    it is also what a refused run falls back to.
    """
    clear_output()
    reply = client.post("/generate", data={
        "token": upload(client), "selected": ["0", "1"],
        "issued_by": "check_runs.py", "venues": ["Saints of Mokha"]})
    assert_status(reply, "/generate")
    if reply.status_code != 200:
        return [f"no JavaScript: /generate answered {reply.status_code}"]
    return every_voucher_is_there("no JavaScript")


def check_refusals_are_messages(client) -> list[str]:
    """A run that will not start says so; it does not fall over."""
    problems = []
    for what, data in [("an expired upload", {"token": "nosuchtoken", "selected": ["0"]}),
                       ("nothing ticked", {"token": upload(client)})]:
        planned = client.post("/generate/plan", data=data)
        assert_status(planned, "/generate/plan")
        if not (planned.get_json() or {}).get("error"):
            problems.append(f"refusals: {what} did not come back as a message")
        assert_status(client.post("/generate", data=data), "/generate")
    stepped = client.post("/generate/step", data={"job": "nosuchjob", "after": "0"})
    assert_status(stepped, "/generate/step")
    if not (stepped.get_json() or {}).get("error"):
        problems.append("refusals: an unknown run did not come back as a message")
    return problems


CHECKS = [
    ("a whole run comes out complete", check_a_whole_run),
    ("no one request draws everything", check_no_request_draws_everything),
    ("asking for a slice twice draws it once", check_asking_twice_draws_once),
    ("a run survives the process being replaced", check_it_survives_a_restart),
    ("a run that stops says what is real", check_a_run_that_stops),
    ("walking away leaves no empty folder", check_no_empty_folders),
    ("a short sheet stops the run", check_a_short_sheet_stops_the_run),
    ("the issued stamp names its zone", check_the_stamp_names_its_zone),
    ("a browser with no JavaScript still works", check_no_javascript),
    ("a run that will not start says so", check_refusals_are_messages),
]


# --------------------------------------------------------------------------
# The two that need a Python of their own
# --------------------------------------------------------------------------

def finish_a_run(job_id: str) -> int:
    """Pick up a run this process knows nothing about and see it through."""
    job = webapp.load_job(job_id)
    if not job:
        print(f"  no job {job_id} to pick up")
        return 1
    state, _ = run_to_the_end(webapp.app.test_client(), webapp._job_state(job))
    return 0 if state["finished"] else 1


def run_without_pymupdf() -> int:
    """Whether PyMuPDF imports is settled once per process, so this is its own."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name in ("pymupdf", "fitz"):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    for name in ("pymupdf", "fitz"):
        sys.modules.pop(name, None)
    builtins.__import__ = blocked
    try:
        if core.pdf_merge_available():
            print("  PyMuPDF still importable; the check proves nothing")
            return 1
        clear_output()
        client = webapp.app.test_client()
        state, sizes = run_to_the_end(client, plan(client, upload(client)))
        if state["done"] != TOTAL:
            print(f"  drew {state['done']} of {TOTAL}")
            return 1
        if len(sizes) != len(EVENTS):
            print(f"  {len(sizes)} slices for {len(EVENTS)} events; without "
                  "PyMuPDF each event must be drawn in one piece")
            return 1
    finally:
        builtins.__import__ = real_import
        for name in ("pymupdf", "fitz"):
            sys.modules.pop(name, None)

    # Counting the pages needs the library this was pretending not to have.
    problems = every_voucher_is_there("without PyMuPDF")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


def check_without_pymupdf(_client=None) -> list[str]:
    out = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--no-pymupdf"]
        + (["--quick"] if QUICK else []),
        capture_output=True, text=True, cwd=str(HERE), env=dict(os.environ))
    if out.returncode != 0:
        return ["without PyMuPDF: " + (out.stderr or out.stdout).strip()[-400:]]
    return []


# --------------------------------------------------------------------------

def main() -> int:
    print()
    print("  DMU voucher generator: making a run, end to end")
    print(f"  Writing to {SCRATCH}")
    print()
    with core.PdfWriter() as writer:
        engine = writer.engine
    print(f"  Drawing with: {engine}")
    print(f"  Sheets can be rejoined: {core.pdf_merge_available()}")
    print(f"  Making {TOTAL} vouchers across {len(EVENTS)} events, each time"
          + (", quick" if QUICK else ""))
    if not QUICK:
        print("  On a free PythonAnywhere account use --quick: at this size the")
        print("  checks together draw more than the day's CPU allowance, and")
        print("  running out slows the site down rather than stopping it.")
    print()
    print("  Two of these break a run on purpose, so expect tracebacks above")
    print("  the results. They are the app reporting what it was handed. Read")
    print("  the ok/FAIL list below, not them.")
    print()

    client = webapp.app.test_client()
    failures = []
    for label, check in CHECKS + [("without PyMuPDF, events are drawn whole",
                                   check_without_pymupdf)]:
        try:
            problems = check(client)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - a check that throws has failed
            problems = [f"{label}: {type(exc).__name__}: {exc}"]
        failures += problems
        print(f"    {'FAIL' if problems else 'ok  '}  {label}")
        for problem in problems:
            print(f"            {problem}")

    print()
    if failures:
        print(f"  {len(failures)} problem(s). This is the machinery that keeps a")
        print("  big run from timing out on the server. Do not deploy past this.")
    else:
        print("  All good. A run comes out whole, in pieces small enough that")
        print("  the server cannot hang up on one, and says something true if")
        print("  it stops.")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    child = "--finish" in sys.argv or "--no-pymupdf" in sys.argv
    try:
        if "--finish" in sys.argv:
            code = finish_a_run(sys.argv[sys.argv.index("--finish") + 1])
        elif "--no-pymupdf" in sys.argv:
            code = run_without_pymupdf()
        else:
            code = main()
    finally:
        # Only the process that made the folder clears it up, and only if it is
        # still provably a folder this script made in a temp directory.
        if not child and disposable(SCRATCH):
            shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(code)
