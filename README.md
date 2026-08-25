# DMU Food & Drink vouchers

Two pieces that work together.

**The generator** runs on the office computer. Drop the approval CSV in, get
print-ready vouchers out. Nothing leaves the machine.

**The redemption site** runs on the internet. A vendor scans the QR code on a
voucher, signs in once a shift, types the number off the paper, and the site
says green or red and records it.

```
approval CSV -> generator -> printed vouchers + an import file
                                   |
                            import file uploaded
                                   |
                          redemption site <- vendor scans, types number
                                   |
                            redemptions CSV -> reconcile
```

---

## Part one: the generator

**Windows:** double-click `run.bat`
**Mac:** double-click `run.command`

A black window opens and your browser goes to the app. Leave the black window
open while you are using it, and close it when you are finished.

The first run takes about twenty seconds longer than the rest, because it draws
the whole run of 500,000 voucher numbers. That happens once, ever.

### Using it

1. **Choose the approval export.** The CSV downloaded from the voucher request
   form. Only rows showing as **Approved** produce vouchers. Anything ignored is
   listed on screen with the reason, so nothing disappears quietly.
2. **Check what was found.** One line per approved request, with the number of
   vouchers, the value each and the total. Click **Preview** to see the print
   sheet before committing to it. A preview uses specimen numbers starting `000`
   and takes nothing out of the pool.
3. **Set the dates.** "Valid until" is printed on the voucher and the site
   refuses the voucher after that date. The event date is optional.
4. **Make the vouchers.**
5. **Upload the import file to the redemption site.** Until you do, the site
   does not know those numbers exist and will refuse them.

### What comes out

Everything lands in `Output\<date> <event name>\`:

| File | What it is |
| --- | --- |
| `Print sheet.pdf` | A4 sheets, six vouchers per page, dashed cut guides. **Print at 100% scale, not "fit to page".** |
| `Individual\DMU-482-173-906.pdf` and so on | One PDF per voucher, named by its number, for emailing to attendees. |
| `Import to redemption site.json` | **Upload this to the site's admin page** or the vouchers will not work. |
| `Vendor instructions.pdf` | One page to give Saints of Mokha and the street food vendor. |
| `Batch summary.csv` | Every number in the batch, with blank columns for recording redemptions by hand if the site cannot be reached. |
| `Source export - ....csv` | A copy of the exact file that was dropped in. |

### Wording and the link

`config.json`, in Notepad. The venue names, the small print, the instruction
lines, and `redeem_url`, which is the address the QR code opens. Save the file
and reload the page in the browser. Nothing else needs touching.

Keep `redeem_url` short. It is the whole content of the QR code, and a long one
prints too fine to scan. The app warns you if it has gone too far: below about
0.5mm per square, phone cameras start losing it on ordinary office paper.

### Logos

Drop `dmu-logo.png` and `food-and-drink-logo.png` into the `assets` folder and
the vouchers will use them. Until then the header uses a plain text stand-in, so
the tool still works without them.

---

## The voucher numbers

The QR code is **the same on every voucher**. It only opens the site. What says
which voucher is which is the number printed in the red box:

```
DMU-482-173-906
```

Nine digits. The first eight are drawn at random from 10,000,000 to 99,999,999.
The ninth is a check digit, which is why a mis-keyed number says "that is not a
voucher number" instead of quietly finding somebody else's voucher. It catches
every single-digit slip and every swap of two neighbouring digits.

500,000 of them were drawn when the app first ran, shuffled, and handed out in
that order. So:

- Two vouchers on the same printed sheet are nowhere near each other
  numerically, and neither can be guessed from the other.
- The same number is never issued twice. The pool records what has gone out.
- A number that starts `000` is a specimen from a preview or the vendor handout,
  never a real voucher.

**The pool is drawn once and must never be drawn again.** The numbers are
printed on paper and cannot be recalled. The app refuses to redraw a pool that
already exists, which is the guard that stops it happening by accident.

### Topping up the numbers

The app warns when fewer than 1,000 are left, and refuses a run that would need
more than remain. At six vouchers a week it will not come up this decade. If it
ever does, do not redraw the pool, add to it. From the generator folder with the
app closed:

```bash
python top_up_pool.py 100000
```

Run it with no number to just see how many are left. It appends new numbers
after the existing ones and checks them against what has already been drawn, so
nothing already printed can reappear.

---

## Part two: the redemption site

Flask and SQLite, nothing else. It runs on a free PythonAnywhere account, and
it will run anywhere else that runs Python if DMU IT would rather host it.

### Setting it up on PythonAnywhere

1. **Get the code there.** In a Bash console:
   ```bash
   git clone https://github.com/insiderpro123/dmu-vouchers.git
   ```
2. **Make a web app.** Web tab, Add a new web app, Manual configuration,
   Python 3.10 or newer.
3. **Point it at the app.** In the WSGI configuration file, replace everything
   with:
   ```python
   import sys
   sys.path.insert(0, "/home/YOURNAME/dmu-vouchers/redeem")
   from wsgi import application
   ```
4. **Set the passwords.** Web tab, Environment variables. Do not put these in a
   file.

   | Name | What it is |
   | --- | --- |
   | `DMU_VENDOR_PASSWORD` | What the vendors are told. Change it when staff leave. |
   | `DMU_ADMIN_PASSWORD` | For DMU Venues. Not the same as the vendor one. |
   | `DMU_SECRET_KEY` | Any long random string. `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `DMU_DB_PATH` | `/home/YOURNAME/dmu-voucher-data/redemptions.db` |

   **Set `DMU_DB_PATH` to somewhere outside the cloned folder.** If the database
   sits inside the repository, a `git pull` can take every redemption with it.

5. **Install Flask**, in a Bash console:
   ```bash
   pip3.10 install --user -r ~/dmu-vouchers/redeem/requirements.txt
   ```
6. **Reload** on the Web tab, then open the site. If anything is missing it
   tells you which variable, rather than failing quietly.
7. **Put the address into `config.json`** on the office computer, as
   `redeem_url`. Then make the vouchers.

Until step 7 is done, vouchers print with an empty QR box. That is deliberate:
better a blank box than a QR code pointing nowhere, printed on 500 vouchers.

### The free account expires. Diarise it

A free PythonAnywhere web app **expires after one month** and stops serving
until somebody logs in and presses **Run until 3 months from today** on the Web
tab. PythonAnywhere shortened this from three months to one in January 2026.

This is the single biggest operational risk in the whole scheme. Vouchers are
printed paper with a fixed QR code on them. If the web app lapses while
vouchers are in circulation, every vendor who scans one gets nothing, and there
is no way to tell the students. The vouchers do not stop being valid, so DMU
still owes the money, but nobody can record a redemption.

So either:

* put a recurring reminder in a shared calendar, not one person's, and press
  the button every month whether or not the email arrives; or
* pay for the Developer tier, where web apps do not expire. At roughly $5 a
  month, this costs less than one voucher and removes the risk entirely.

The paid tier also allows a custom domain, so the QR could point at something
like `dmu.insiderpro.co.uk` instead of a `pythonanywhere.com` address.

Two other free-tier limits, neither of which affects this site: MySQL and
scheduled tasks are now Developer-tier only. The redemption site uses neither,
just SQLite on the filesystem.

### Running it locally to try it

```bash
python redeem/app.py
```

Copy `redeem/.env.example` to `redeem/.env` and fill it in first. `.env` is
gitignored and never leaves the machine.

### What the vendor sees

Sign in once at the start of a shift, choosing which venue they are. Then, per
customer: type the number, read green or red, press Redeem.

- **Green** with the value in large type. Redeem it.
- **Red** for already redeemed (it says where and when), expired, cancelled, or
  not recognised. No Redeem button is offered at all.

Checking a number never uses the voucher up. The vendor sees the value first and
presses Redeem separately, so a mistyped number cannot burn a voucher.

Twelve wrong numbers from one venue in ten minutes throttles that venue for a
few minutes. Vendors mistype; that limit is nowhere near the mistyping rate, but
it makes guessing at numbers pointless.

### What DMU Venues sees

`/admin`, with the admin password:

- **Import a batch** from the generator's `Import to redemption site.json`.
  Importing the same file twice is harmless.
- **Totals**: issued, redeemed, redeemed value, outstanding value.
- **Per batch and per venue** breakdowns.
- **Find a voucher** to see its history.
- **Undo a redemption** made in error. The voucher becomes usable again and the
  undo stays on the record, with the reason, rather than the redemption
  vanishing.
- **Cancel a voucher** reported lost. Vendors are told to refuse it.
- **Download everything as CSV** to reconcile.

---

## Reconciling

Two records, kept deliberately apart, because the office computer is somebody's
laptop and the site has to work when it is shut.

- The generator's `Ledger\issued_vouchers.csv` says what went out.
- The site's admin CSV says what came back.

Download the site's CSV and compare on the voucher number. Every number in the
ledger appears in the site's CSV once a batch is imported, marked Unused,
Redeemed, Expired unused or Cancelled.

If a number is in the ledger but not the site's CSV, that batch was never
imported.

---

## If something goes wrong

**The generator:** the black window behind the browser shows what happened. If
it mentions `playwright` or `chromium`, close everything and run `run.bat`
again; it reinstalls what it needs.

**A failed print run takes no numbers.** If PDF generation falls over, whatever
was taken out of the pool goes straight back, because nothing was printed.

**The site says a voucher is not recognised.** Almost always the batch was never
imported. Check the Batches table on the admin page.

**The site says it is not set up yet.** An environment variable is missing. The
page names which one.

**Do not run the generator on two machines at once.** The pool is a single file
in Dropbox. Two copies writing to it at the same time is how it gets corrupted.

---

## Files

```
app.py                  the generator
vouchers.py             voucher logic, PDF output
refs.py                 the number scheme and the check digit   } shared
store.py                the database                            } by both
config.json             wording and the redemption site address
Ledger/
  issued_vouchers.csv   the audit trail, openable in Excel
  vouchers.db           the pool and what has gone out (30MB, not in git)
redeem/
  app.py                the redemption site
  settings.py           reads the environment variables
  wsgi.py               what PythonAnywhere loads
  .env.example          copy to .env for local testing
```

`refs.py` and `store.py` are shared by both apps on purpose. The check digit has
to mean the same thing in both places or vouchers stop working, so there is one
copy of it.
