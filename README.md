# DMU Food & Drink vouchers

Drop the approval CSV in, get print-ready vouchers out.

Vouchers are made **on the site**, behind one password. The same code runs on
the office computer from `run.bat`, which is how the vendor sheet's picture gets
remade and how anything is tried out before it goes up, but vouchers are issued
in one place only, so that one ledger holds every number ever printed.

```
approval CSV  ->  the app  ->  printed vouchers
                                     |
                          Ledger\issued_vouchers.csv
                            (what went out, for reconciling)
```

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
2. **Check what was found.** One line per approved request, with the number of
   vouchers, the value each and the total. Click **Preview** to see the print
   sheet before committing to it. A preview uses specimen numbers starting `000`
   and writes nothing to the ledger.
3. **Set the dates.** "Valid until" is printed on the voucher. The event date is
   optional.
4. **Make the vouchers.**

That is the whole job. There is nothing to upload afterwards and nothing to sign
in to.

## What comes out

Everything lands in `Output\<date> <event name>\`:

| File | What it is |
| --- | --- |
| `Print sheet.pdf` | A4 sheets, six vouchers per page, dashed cut guides. **Print at 100% scale, not "fit to page".** |
| `Individual\DMU-482-173-906.pdf` and so on | One PDF per voucher, named by its number, for emailing to attendees. |
| `All vouchers (individual pages).pdf` | The same one-per-page vouchers as a single document, if you would rather print than email. |
| `Vendor instructions.pdf` | One page to give Saints of Mokha and the street food vendor. |
| `Batch summary.csv` | Every number in the batch, with blank columns for recording redemptions by hand. |
| `Source export - ....csv` | A copy of the exact file that was dropped in. |

And `Ledger\issued_vouchers.csv` gains a row per voucher. That file is the record
of everything ever issued. Do not delete it: it is both the audit trail and what
the app checks to make sure a number is never issued twice.

## Wording, venues and the QR link

`config.json`, in Notepad. The venue names, the small print, the instruction
lines, and `qr_url`, which is the address the QR code opens. Save the file and
reload the page in the browser. Nothing else needs touching.

**There is no QR code on a voucher.** The code is the same for all 500 of them,
so printing it 500 times was waste and the vendor bookmarks the page after one
scan anyway. It is printed once, at 45mm, on `Vendor instructions.pdf`. If
`qr_url` is still a placeholder, that sheet prints with a note where the code
should be; the vouchers themselves are unaffected.

The address does **not** need shortening. On an A4 sheet the code gets 45mm
rather than the 27mm a voucher could spare, which takes the long
`ResponsePage.aspx` link to 0.92mm per square against a floor of about 0.5mm,
below which phone cameras start losing it on ordinary office paper. The app
tells you the figure on the front page and warns you if a link ever does go too
far.

## Logos

Drop `dmu-logo.png` and `food-and-drink-logo.png` into the `assets` folder and
the vouchers will use them. Until then the header uses a plain text stand-in, so
the tool still works without them.

**After adding the logos, run `make_sample_thumbnail.py`** (see below). The
vouchers pick the logos up on their own; the picture on the vendor sheet does
not, because it is a photograph.

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

---

## The voucher numbers

There is no QR code on a voucher, so the number printed in the red panel is the
**only** thing that says which voucher this is:

```
DMU-482-173-906
```

Nine digits. The first eight are drawn at random from 10,000,000 to 99,999,999,
so 90 million possibilities against the few thousand that get issued. The ninth
is a check digit, which is what lets a mis-typed number be told apart from one
that was never issued. It catches every single-digit slip and every swap of two
neighbouring digits.

Each number is drawn when its batch is made, and checked against the ledger, so:

- Two vouchers on the same printed sheet are nowhere near each other
  numerically, and neither can be guessed from the other.
- The same number is never issued twice.
- A number starting `000` is a specimen from a preview or the vendor handout,
  never a real voucher.

There is nothing to set up, nothing to run out of and nothing to top up.

## Reconciling

`Ledger\issued_vouchers.csv` says what went out. Compare it, on the voucher
number, against however redemptions are being collected: the Microsoft Form's
response spreadsheet, or the `Batch summary.csv` sheets if they were filled in on
paper.

A number in the responses that is not in the ledger was either mis-typed or was
never a voucher. The same number appearing twice in the responses is a voucher
that was used twice, which is why vendors are told to keep the paper.

## If something goes wrong

**The black window behind the browser says what happened.** If it mentions
`playwright` or `chromium`, close everything and run `run.bat` again; it
reinstalls what it needs. `python check_pdf_engine.py` tests PDF output on its
own if you need to look closer.

**A failed run leaves nothing behind.** Numbers are written to the ledger only
after the PDFs exist, so a run that falls over has nothing to undo.

**Do not issue vouchers from the office copy.** The site keeps its own ledger,
on the server, and the two cannot see each other: a number issued here is
invisible to the check that stops the site printing the same one again.
Previewing and `make_sample_thumbnail.py` are both safe, because neither writes
to a ledger.

## On the server

The site is this same app with three things set: a password, a data folder
outside the repository, and WeasyPrint in place of Chromium. All three are read
by `wsgi.py`, which is the file PythonAnywhere loads.

**Setting it up**

1. **Get the code there.** Clone or upload the folder to
   `/home/YOURNAME/dmu-vouchers`.
2. **Install what it needs.** In a Bash console,
   `pip3.10 install --user -r requirements-server.txt`. No Playwright: Chromium
   is 427 MB against a free account's 512 MB.
3. **Point the web app at it.** Web tab, WSGI configuration file, replace what
   is in it with the two lines at the top of `wsgi.py`.
4. **Set the password.** Copy `.env.example` to `.env` in the same folder and
   fill in `DMU_SITE_PASSWORD`. That file is gitignored and never leaves the
   server. There is **no Environment variables section** in the Web tab, whatever
   older notes in this repository claimed.
5. **Carry the ledger over.** Upload `Ledger\issued_vouchers.csv` to
   `~/dmu-voucher-data/Ledger/issued_vouchers.csv`. Miss this step and the site
   starts from an empty record, which means it can reissue a number that has
   already been printed.
6. **Press Reload.** Nothing takes effect until you do. This is the step that
   looks like "the change did not work".

**Deploying a change**

Pull or upload the changed files, then press Reload. The records live in
`~/dmu-voucher-data`, deliberately outside the repository folder, so a deploy
cannot take the ledger with it.

**What is different up there**

- One password prompt in front of everything. With `DMU_SITE_PASSWORD` unset the
  app serves nothing at all, rather than serving an open voucher printer to
  whoever finds the address.
- Finished batches come down as a zip, because there is no folder to open.
  Download them and keep them: the copy on the server is not a backup.
- PDFs are drawn by WeasyPrint rather than Chromium, from the same HTML and CSS.
  Check the first print sheet against a local one before a real print run.
- The vendor sheet's example picture cannot be remade there, because that needs
  Chromium. Run `make_sample_thumbnail.py` on the office computer and deploy the
  PNG it makes.

## Files

```
run.bat / run.command   what you double-click
app.py                  the web pages and the CSV drop
vouchers.py             voucher logic, PDF output
refs.py                 the number scheme and the check digit
config.json             wording, venues, the QR link
wsgi.py                 what the server loads, and what configures it
.env.example            copy to .env on the server: the password lives there
requirements-server.txt what to install on the server
check_pdf_engine.py     tests PDF output when it will not work
make_sample_thumbnail.py  remakes the vendor sheet's example picture
static/
  voucher.css           how a voucher looks, screen and print alike
  sample-voucher.png    the example picture on the vendor sheet
Ledger/
  issued_vouchers.csv   the record of everything issued
Output/                 a folder per batch
assets/                 the two logo PNGs
```

The redemption site that used to live in `redeem/`, and the pre-drawn pool of
500,000 numbers that went with it, were removed in August 2026 when the QR code
moved to a Microsoft Form. Both are in the git history if they are ever wanted
back.
