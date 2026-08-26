"""Voucher references: the numbers printed on the vouchers.

The number is what identifies a voucher. The QR code on the artwork is the same
on every one of them, so it says nothing about which voucher it is.

That puts two demands on the number that pull against each other. It has to be
quick and forgiving to read off paper and type, and it has to be sparse enough
that nobody guesses a live one. The shape below is the compromise:

    DMU-482-173-906

Nine digits, grouped in threes like a phone number. The first eight are drawn at
random from 10,000,000 to 99,999,999, so 90 million possibilities against the
few thousand that get issued. The ninth is a Damm check digit, which catches
every single-digit slip and every transposition of neighbouring digits, so a
mis-keyed number can be told apart from one that was never issued.
"""

from __future__ import annotations

import random
import re

PREFIX = "DMU"
PAYLOAD_DIGITS = 8
TOTAL_DIGITS = PAYLOAD_DIGITS + 1  # payload plus the check digit

PAYLOAD_LOW = 10 ** (PAYLOAD_DIGITS - 1)          # 10000000, no leading zero
PAYLOAD_HIGH = 10 ** PAYLOAD_DIGITS - 1           # 99999999


# ---------------------------------------------------------------------------
# Damm check digit
# ---------------------------------------------------------------------------

# The standard totally anti-symmetric quasigroup of order 10. Do not reorder it:
# the anti-symmetry is what catches transpositions, and a "tidied up" table
# silently stops catching them while still looking like it works.
_DAMM = (
    (0, 3, 1, 7, 5, 9, 8, 6, 4, 2),
    (7, 0, 9, 2, 1, 5, 4, 8, 6, 3),
    (4, 2, 0, 6, 8, 7, 1, 3, 5, 9),
    (1, 7, 5, 0, 9, 8, 3, 4, 2, 6),
    (6, 1, 2, 3, 0, 4, 5, 9, 7, 8),
    (3, 6, 7, 4, 2, 0, 9, 5, 8, 1),
    (5, 8, 6, 9, 7, 2, 0, 1, 3, 4),
    (8, 9, 4, 5, 3, 6, 2, 0, 1, 7),
    (9, 4, 3, 8, 6, 1, 7, 2, 0, 5),
    (2, 5, 8, 1, 4, 3, 6, 7, 9, 0),
)


def damm_check_digit(digits: str) -> int:
    interim = 0
    for ch in digits:
        interim = _DAMM[interim][int(ch)]
    return interim


# ---------------------------------------------------------------------------
# Formatting and parsing
# ---------------------------------------------------------------------------

def format_reference(digits: str) -> str:
    """'482173906' -> 'DMU-482-173-906'."""
    if len(digits) != TOTAL_DIGITS:
        raise ValueError(f"expected {TOTAL_DIGITS} digits, got {len(digits)}")
    return f"{PREFIX}-{digits[0:3]}-{digits[3:6]}-{digits[6:9]}"


def digits_of(reference: str) -> str:
    """The bare digits of a reference, however it was written down."""
    return re.sub(r"\D", "", reference or "")


def make_reference(payload: int) -> str:
    """Turn a payload number into the printed reference."""
    body = str(payload).zfill(PAYLOAD_DIGITS)
    return format_reference(body + str(damm_check_digit(body)))


# ---------------------------------------------------------------------------
# Drawing numbers
# ---------------------------------------------------------------------------

def draw_references(count: int, taken: set[str], rng=None) -> list[str]:
    """Draw `count` references nobody has had before.

    Random rather than in sequence, so two vouchers on the same printed sheet
    are nowhere near each other numerically and neither can be guessed from the
    other.

    90 million possible payloads against the few thousand that get issued means
    a repeat is very unlikely, but `taken` catches one if it happens rather than
    leaving it to the odds. Pass an rng only to make a test repeatable; left
    alone it draws from the system entropy source.
    """
    if count < 0:
        raise ValueError("cannot draw a negative number of references")
    rng = rng or random.SystemRandom()
    drawn: list[str] = []
    seen = set(taken)
    while len(drawn) < count:
        reference = make_reference(rng.randint(PAYLOAD_LOW, PAYLOAD_HIGH))
        if reference in seen:
            continue
        seen.add(reference)
        drawn.append(reference)
    return drawn
