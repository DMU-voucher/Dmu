# DMU Food & Drink vouchers

One app on the office computer. Drop the approval CSV in, get print-ready
vouchers out. Nothing leaves the machine.

```
approval CSV  ->  the app  ->  printed vouchers
                                     |
                          Ledger\issued_vouchers.csv
                            (what went out, for reconciling)
```

---

## Running it

**Windows:** double-click `run.bat`
**Mac:** double-click `run.command`

A black window opens and your browser goes to the app. Leave the black window
open while you use it, and close it when you are done.

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

`qr_url` is currently a placeholder, so **vouchers print with an empty QR box.**
That is fine for checking the layout and not for issuing. Put the Microsoft Form
address in when the form exists.

Keep that address short. It is the whole content of the QR code and a long one
prints too fine to scan. In Microsoft Forms, open **Collect responses** and copy
the short `forms.office.com/r/` link rather than the long `ResponsePage.aspx`
one. The app warns you if it has gone too far: below about 0.5mm per square,
phone cameras start losing it on ordinary office paper.

`vendor_record` is the one line on the vendor sheet that tells vendors what to do
with the number. Put the form address in there too when you have it.

## Logos

Drop `dmu-logo.png` and `food-and-drink-logo.png` into the `assets` folder and
the vouchers will use them. Until then the header uses a plain text stand-in, so
the tool still works without them.

---

## The voucher numbers

The QR code is **the same on every voucher**. What says which voucher is which is
the number printed in the red box:

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

**Do not run the app on two machines at once.** The ledger is a single file in
Dropbox, and two copies appending to it at the same time is how it gets damaged.

## Files

```
run.bat / run.command   what you double-click
app.py                  the web pages and the CSV drop
vouchers.py             voucher logic, PDF output
refs.py                 the number scheme and the check digit
config.json             wording, venues, the QR link
check_pdf_engine.py     tests PDF output when it will not work
Ledger/
  issued_vouchers.csv   the record of everything issued
Output/                 a folder per batch
assets/                 the two logo PNGs
```

The redemption site that used to live in `redeem/`, and the pre-drawn pool of
500,000 numbers that went with it, were removed in August 2026 when the QR code
moved to a Microsoft Form. Both are in the git history if they are ever wanted
back.
