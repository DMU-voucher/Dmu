# DMU Food & Drink vouchers

Drop the approval CSV in, get print-ready vouchers out.

Vouchers are made **on the site**, behind one password. The same code runs on
the office computer from `run.bat`, which is how the vendor sheet's picture gets
remade and how anything is tried out before it goes up.

```
approval CSV  ->  the app  ->  a folder per request
                                (print sheet + summary)
```

**This is a printing tool and it keeps no record between runs.** Nothing is
logged, nothing is checked against a previous run, and the same request can be
printed as many times as you like. It prints the same vouchers every time, since
a code is the export's ID and a ticket number rather than something drawn fresh.
Download each batch when it is made and keep it: the folder is the record.

---

## Running it

**The site.** Open it and type the password. That is where vouchers are made.
Setting it up is under "On the server" below.

**The office computer,** for previewing and for remaking the vendor sheet's
picture. Double-click `run.bat` on Windows or `run.command` on Mac. A black
window opens and your browser goes to the app. Leave it open while you use it,
and close it when you are done. Do not issue real vouchers from here: see the
warning at the end.

## Making vouchers

1. **Choose the approval export.** The CSV downloaded from the voucher request
   form. Only rows showing as **Approved** produce vouchers. Anything ignored is
   listed on screen with the reason, so nothing disappears quietly.
2. **Check what was found.** One line per approved request, with the DMU ID, the
   number of vouchers, the value each, the total, and **both dates as they came
   out of the file**. Click **Preview** to see the print sheet before committing
   to it. A preview uses specimen numbers starting `000` and writes nothing
   anywhere.
3. **Choose where they can be spent.** Every venue in `config.json` is offered,
   ticked. Untick any that do not apply to this run, and type a one-off vendor in
   the box if there is one. The one-off prints but is not remembered.
4. **Make the vouchers.**

That is the whole job. There is nothing to upload afterwards and nothing to sign
in to.

### The dates come from the file, not from you

There is no date field on the page. **Expiry Date** and **Event Date** are read
per row from the export, so a drop covering four events with four different
expiry dates comes out right. Before, one date was typed once and applied to
everything in the run, which is only correct when the run is one event.

Two consequences worth knowing:

- **A row with no expiry date is not offered.** It appears under "Rows not used"
  saying so. There is nowhere to type one in, and a voucher printed without an
  expiry never expires while the vendor sheet tells vendors to refuse an expired
  one. Put the date in the export and drop it in again.
- **The event date is optional** and simply stays off the voucher when the cell
  is blank. It is written into the batch summary either way, which is where the
  weekly "when will you be busy" list for vendors comes from.

### The voucher code

The code in the red box is DMU's own: the export's **ID** column, a hyphen, and
the voucher's place in that request. Request 12 for 40 vouchers prints `12-01` to
`12-40`. It is on the voucher and in the batch summary, and it
is the only thing that says which voucher this is.

**A row with no ID is refused**, and appears under "Rows not used" saying so.
There is no code without an ID, and a voucher with an empty box is one nobody can
record a redemption against.

## What comes out

**One request, one folder, one zip.** The folder is named with the ID, so it can
be sent to whoever asked for the vouchers exactly as it stands, and every code
inside it starts with that same ID:

```
Output\2026-08-28 ID 12 Test 5\
```

| File | What it is |
| --- | --- |
| `Print sheet - Test 5 ID 12.pdf` | A4 sheets, six vouchers per page, dashed cut guides. **Print at 100% scale, not "fit to page".** |
| `Batch summary - Test 5 ID 12.csv` | Every code in the batch, plus blank columns for recording redemptions by hand. Also records the DMU ID and which vendors the run was for. |

Two files, both named for the event and its ID, so a folder can be sent to
whoever asked for the vouchers exactly as it stands and two batches cannot be
confused once they are unzipped.

Two things that used to be in here are not any more:

- **The vendor sheet.** It is the same single page for every batch in a run, and
  it goes to vendors rather than to the requestor. Download it from the link on
  the done page or the bottom of the front page.
- **A copy of the approval export.** It was the whole uploaded file, so sending
  one requestor their folder showed them every other request in it, with values,
  cost centres and approvers. The file that was dropped in is still kept in
  `Uploads\` for the audit trail, and the summary carries that row's own detail.

And that is all that is kept. **There is no log.** The app used to write every
code to `Ledger\issued_vouchers.csv` and refuse to print a request twice, and
both went in August 2026 at DMU's request: this is a printing platform, and
being told a lost sheet could not be reprinted was getting in the way more than
it was protecting anything. Nothing checks a run against a previous one, so
reprinting is simply printing again.

An old `Ledger\issued_vouchers.csv` from before that change is not read, written
or deleted by the app. It is client data. Keep it or remove it deliberately.

## Wording, venues and the QR link

`config.json`, in Notepad. The venue names, the small print, the instruction
lines, and `qr_url`, which is the address the QR code opens. Save the file and
reload the page in the browser. Nothing else needs touching.

**Venues are the settled list.** Everything in `venues` is offered on the
make-vouchers page, ticked, and a run can untick any of them or add a one-off in
the box beside them. Put a vendor here when it is permanent; use the box when it
is a pop-up. Unticking every venue prints the full list rather than a voucher
with nowhere to spend it.

Adding or renaming a venue changes what a voucher looks like, so the example
picture on the vendor sheet goes out of date and has to be remade. See below.

**There is no QR code on a voucher.** The code is the same for all 500 of them,
so printing it 500 times was waste and the vendor bookmarks the page after one
scan anyway. It is printed once, at 45mm, on the vendor sheet. If
`qr_url` is still a placeholder, that sheet prints with a note where the code
should be; the vouchers themselves are unaffected.

The address does **not** need shortening. On an A4 sheet the code gets 45mm
rather than the 27mm a voucher could spare, which takes the long
`ResponsePage.aspx` link to 0.92mm per square against a floor of about 0.5mm,
below which phone cameras start losing it on ordinary office paper. The app
tells you the figure on the front page and warns you if a link ever does go too
far.

## The logo

`assets/dmu-logo.png` is DMU's horizontal lockup, and every voucher and every
page in the app carries it. It is the one thing in `assets` that git tracks: the
deploy is a `git pull` with no upload step, so a logo that stayed out of the
repository would leave the live site printing the text stand-in. The file DMU
supplied sits next to it as `dmu-logo-original.png`, which git does ignore.

Food and Drink has no mark of its own, so there is nothing to wait for and no
stand-in for one. The words still read on the artwork: `voucher_title` in
`config.json` sets "Food & Drink Voucher" beside the value.

The logo is 8mm tall on the voucher, and that number is measured rather than
chosen. The voucher is a fixed 99mm cell whose foot cannot grow, so every
millimetre the header takes comes out of the box holding the voucher code. At
9mm the padding round the code drops under a millimetre; at 10mm it closes up.
`LEICESTER` prints small as a result. That is the price of keeping the code at
27pt, which is what a vendor keys in with a queue waiting.

**If you replace the logo, run `make_sample_thumbnail.py` and then
`check_pdf_engine.py`.** The vouchers pick a new file up on their own, but the
picture on the vendor sheet does not, and a taller logo can push the artwork out
of its cell.

## The picture on the vendor sheet

The handout shows an example of what a voucher looks like. That picture is a PNG
in `static`, taken once by `make_sample_thumbnail.py`, not the live artwork
shrunk down on the page. It has to be a picture: the server renders with
WeasyPrint, which keeps a shrunken voucher's full layout size and paginates the
bottom of it away, so the drawn version came out with an empty voucher-number
box. A picture renders the same in both engines.

Run it on the office computer, which is the one with Chromium:

```
python make_sample_thumbnail.py
```

Then commit `static/sample-voucher.png` and `static/sample-voucher.json`, or the
server keeps the old picture.

Run it again after anything that changes how a voucher looks: `voucher.css`,
`_voucher.html`, the voucher wording in `config.json`, or the logos. **You do not
have to remember.** The app fingerprints all of those, and the front page tells
you when the picture no longer matches, as does `check_pdf_engine.py`. The
vouchers themselves are never affected, only the example on the handout.

**If the site says the picture is out of date, check that the picture really is
the problem.** The banner only means the server's fingerprint does not match the
stamp in `sample-voucher.json`, which is not the same as the handout being
wrong: the sheet uses `sample-voucher.png` whenever that file is there, whatever
the banner says. So compare the two copies before remaking anything. In a
PythonAnywhere Bash console,

```
cd ~/dmu-vouchers && git pull && sha256sum static/sample-voucher.png
```

and the same command here. Matching hashes mean the right picture is already
being served and it is the warning that is wrong. The PNG is read from disk on
every request, so a new one counts the moment it lands; the Reload is for code
changes, not for the picture.

**The fingerprint has to ignore line endings, and August 2026 is why.** Git
stores the tracked text files with LF and hands a Windows checkout CRLF, so
`voucher.css` is 16529 bytes on the office computer and 15898 on the server.
`thumbnail_fingerprint` hashed the bytes as they sat on disk, so a stamp written
here could never match the one Linux worked out, and the site called the picture
out of date permanently no matter how many times it was remade. It flattens line
endings before hashing now. If that ever regresses, the symptom is a warning
that running `make_sample_thumbnail.py` will not clear.

---

## The voucher codes

There is no QR code on a voucher, so the code printed in the red panel is the
**only** thing that says which voucher this is:

```
12-01
```

The export's ID, then the ticket number within that request, padded to the width
of the count so they sort: 100 vouchers run `12-001` to `12-100`. Nothing is
drawn or allocated, so a code is simply a property of the request. There is
nothing to set up, run out of or top up.

`00-000` is the specimen used by previews and the vendor handout. No request has
ID 0, so a sheet carrying it can never be passed off as real vouchers.

**This replaced a random nine-digit reference in August 2026, and two things went
with it.** Both were raised and accepted, and both matter when reconciling:

- **There is no check digit.** The old `DMU-482-173-906` ended in one, which
  caught every single-digit slip and every swap of neighbouring digits. Codes now
  run consecutively, so a vendor typing `12-10` instead of `12-01` hits another
  live voucher in the same batch rather than an error. A mistyped code cannot be
  told from a real one.
- **Codes are guessable.** Anyone holding one voucher can write down the rest of
  the batch. What actually prevents reuse is the vendor keeping the paper, which
  the vendor sheet already says in bold.

### How big the code prints, and why it changes

27pt at the settled three venues, stepping down a ladder to 22, 18 and 14pt.
Two things push it down and the harder one wins:

- **A long code.** IDs and counts have no ceiling. `50000-8000` is ten
  characters and `123456789-123456789` is nineteen, and a code does not wrap,
  it runs off the paper.
- **A long venue list.** A print run can add a vendor of its own, and each
  venue is another line on a voucher that cannot get any taller. At four
  venues the artwork gives way in small steps across the value, the
  instruction line, the list and the code together, rather than taking it all
  out of the code.

The sizes are measured rather than chosen: the code box comes out at 17.5mm
with three venues, 16.1mm with four, 15.4mm with five and 13.8mm with six, and
each step of the ladder fits its box with a millimetre and a half to spare.
`check_pdf_engine.py` draws all of those combinations and measures them, so a
change here that stops fitting fails the check rather than reaching paper.

**A fourth vendor is what broke this in August 2026.** The code box had a 13mm
floor, which is not protection when the thing inside it cannot shrink: the box
stopped at 13mm holding 14.4mm of code, and the difference printed straight
through the small print above. The floor is gone and the code steps down
instead.

## Reconciling

The batch summaries say what went out: one per request, in its folder, listing
every code with blank columns for recording redemptions. Compare them, on the
voucher code, against however redemptions are being collected: the Microsoft
Form's response spreadsheet, or the summaries themselves if they were filled in
on paper.

This only works if the batch folders are kept. Nothing else records what was
printed.

A code in the responses that is in no summary was either mis-typed or was never
a voucher. The same code appearing twice is a voucher that was used twice, which
is why vendors are told to keep the paper. A code is the ID and the ticket
number with no check digit, so a mis-key can land on another real voucher in the
same batch.

## If something goes wrong

**The black window behind the browser says what happened.** If it mentions
`playwright` or `chromium`, close everything and run `run.bat` again; it
reinstalls what it needs. `python check_pdf_engine.py` tests PDF output on its
own if you need to look closer.

**A failed run leaves nothing behind.** The error page says so and means it:
nothing is written anywhere until the PDFs exist.

**Printing twice is allowed.** Nothing checks a run against a previous one, and
the same request prints the same vouchers every time, so a lost or spoiled sheet
is reprinted by simply making it again. That also means two people printing the
same request separately produce two identical sets of vouchers, which only the
paper itself can tell apart.

## On the server

The site is this same app with three things set: a password, a data folder
outside the repository, and WeasyPrint in place of Chromium. All three are read
by `wsgi.py`, which is the file PythonAnywhere loads.

**Setting it up**

1. **Get the code there.** Clone or upload the folder to
   `/home/YOURNAME/dmu-vouchers`.
2. **Install what it needs**, with the pip that matches the Python version the
   Web tab is set to. In a Bash console,
   `pip3.10 install --user -r requirements-server.txt`, substituting the version.
   No Playwright: Chromium is 427 MB against a free account's 512 MB.

   **A web app's Python version is fixed when the web app is created and cannot
   be changed afterwards.** Install with the wrong one and the site serves
   **502-backend with a completely empty error log**, which reads like a broken
   app but is only a missing interpreter. This one-liner finds the right version
   rather than guessing:

   ```
   for p in $(ls -d /usr/bin/python3.[0-9]* | grep -v config); do echo "== $p"; (cd ~/dmu-vouchers && "$p" -c "import wsgi" 2>&1 | tail -2); done
   ```

   Only `flask` and `segno` are imported at startup. WeasyPrint and PyMuPDF are
   imported inside the functions that use them, so neither can cause a boot 502.
3. **Point the web app at it.** Web tab, WSGI configuration file, replace what
   is in it with the two lines at the top of `wsgi.py`.
4. **Set the password.** Copy `.env.example` to `.env` in the same folder and
   fill in `DMU_SITE_PASSWORD`. That file is gitignored and never leaves the
   server. There is **no Environment variables section** in the Web tab, whatever
   older notes in this repository claimed.
5. **Press Reload.** Nothing takes effect until you do. This is the step that
   looks like "the change did not work".

**Deploying a change**

Pull or upload the changed files, press Reload, **then run
`check_pdf_engine.py` on the server before anyone prints a real batch.** That
last step is not optional: it is the one that was missing in August 2026, and
skipping it is how broken artwork reached paper.

The finished batches live in `~/dmu-voucher-data`, deliberately outside the
repository folder, so a deploy cannot take them with it. They are the only
record there is, and the server is not a backup: download each batch.

**Prefer `git pull` to uploading by hand.** Uploading is where
`static/sample-voucher.png` gets left behind, because it is the one file that
looks like an asset rather than code, and a missing picture does not error: the
handout quietly falls back to the drawn artwork, which is the version WeasyPrint
prints with an empty voucher-number box.

**What is different up there**

- One password prompt in front of everything. With `DMU_SITE_PASSWORD` unset the
  app serves nothing at all, rather than serving an open voucher printer to
  whoever finds the address.
- **The username in that prompt is not checked.** The browser asks for one
  because that is what HTTP basic authentication is; only the password matters,
  so type anything in the name box. Two names were in play at once in August
  2026, the app's and PythonAnywhere's account-level protection, and since the
  prompt gives no clue which is being asked for, signing in turned into a
  guessing game that locked the site's owner out. If PythonAnywhere's own
  password protection is switched off, this prompt is the only door to the
  voucher printer, so the password should be a real one and should be changed
  after it has been typed anywhere it might be seen.
- Finished batches come down as a zip, because there is no folder to open.
  Download them and keep them: the copy on the server is not a backup.
- PDFs are drawn by WeasyPrint rather than Chromium, from the same HTML and CSS.
  **The two do not agree, and Chromium passing on the office machine proves
  nothing about the server.** In August 2026 a layout that was perfect locally
  shipped with the voucher code printed on top of the event name and the small
  print sliced in half by the code box, because nothing had ever rendered it with
  WeasyPrint.

  So, **after every deploy and before printing anything real**, run

  ```
  python3.10 check_pdf_engine.py
  ```

  on the server. It renders the artwork with whichever engine is there and then
  measures the result for overlapping text, text escaping its voucher and text
  printed over the code box, and exits non-zero if it finds any. It also writes
  its PDFs to `engine-check/`, which can be downloaded from the Files tab.

  Two WeasyPrint gaps to design around, both found the hard way:
  `text-overflow: ellipsis` does nothing, and auto margins on flex items are not
  honoured. `voucher.css` must not rely on either.
- The vendor sheet's example picture cannot be remade there, because that needs
  Chromium. Run `make_sample_thumbnail.py` on the office computer and deploy the
  PNG it makes.

## Files

```
run.bat / run.command   what you double-click
app.py                  the web pages and the CSV drop
vouchers.py             voucher logic, PDF output
config.json             wording, venues, the QR link
wsgi.py                 what the server loads, and what configures it
.env.example            copy to .env on the server: the password lives there
requirements-server.txt what to install on the server
check_pdf_engine.py     renders the artwork and measures it. Run it on the server
make_sample_thumbnail.py  remakes the vendor sheet's example picture
static/
  voucher.css           how a voucher looks, screen and print alike
  sample-voucher.png    the example picture on the vendor sheet
Output/                 a folder per batch, which is the only record kept
pace.json               how long the last run took, for the progress bar
assets/                 DMU's logo, and the original DMU supplied
```

`pace.json` sits with the records rather than in the repository, because it is a
measurement of the machine it was made on: the office computer draws with
Chromium in a couple of seconds and the server draws the same sheet with
WeasyPrint at its own speed. The progress bar fills against that figure, so the
first run after a fresh install shows a guess and every run after it is
measured. Delete the file and it goes back to the guess. Nothing else reads it,
and losing it costs nothing.

The redemption site that used to live in `redeem/`, and the pre-drawn pool of
500,000 numbers that went with it, were removed in August 2026 when the QR code
moved to a Microsoft Form. Both are in the git history if they are ever wanted
back.
