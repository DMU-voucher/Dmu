"""Voucher references: the numbers printed on the vouchers.

One QR code goes on every voucher and it is the same code on all of them. It
opens the redemption site. What makes each voucher its own thing is the
reference printed underneath, which the vendor keys in after scanning.

That puts two demands on the reference that pull against each other. It has to
be quick and forgiving to type on a phone with a queue waiting, and it has to be
sparse enough that nobody guesses a live one. The shape below is the compromise:

    DMU-482-173-906

Nine digits, grouped in threes like a phone number. The first eight are drawn at
random from 10,000,000 to 99,999,999, so 90 million possibilities for the
500,000 that get issued. The ninth is a Damm check digit, which catches every
single-digit slip and every transposition of neighbouring digits, so a mis-keyed
reference says "that is not a valid reference" rather than quietly finding
somebody else's voucher.

The references are drawn once into a pool and handed out in that shuffled order,
so two vouchers printed side by side are nowhere near each other numerically.
Guessing one from the other tells you nothing.

This module is shared by the generator and the redemption site. Both must agree
on the check digit or vouchers stop scanning, so it lives in one file.
"""

from __future__ import annotations

import random
import re

PREFIX = "DMU"
PAYLOAD_DIGITS = 8
TOTAL_DIGITS = PAYLOAD_DIGITS + 1  # payload plus the check digit
POOL_SIZE = 500_000

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


def damm_valid(digits: str) -> bool:
    """True when the check digit on the end agrees with the rest."""
    return damm_check_digit(digits) == 0


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


# Typed on a phone by someone holding a paper voucher, so be generous about the
# spelling and strict about the number underneath. Letters that people
# substitute for digits get folded back before anything is checked.
_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "I": "1", "i": "1",
                             "l": "1", "L": "1", "S": "5", "s": "5"})


def normalise_typed(raw: str) -> str | None:
    """What the vendor typed -> canonical reference, or None if it cannot be one.

    Accepts 'DMU-482-173-906', 'dmu 482173906', '482 173 906', '482173906'.
    Returns None when the digits do not add up, so a slip never resolves to a
    real but different voucher.
    """
    if not raw:
        return None

    text = raw.strip().translate(_LOOKALIKES)
    text = re.sub(r"(?i)^\s*dmu\b", "", text)  # optional prefix
    body = re.sub(r"\D", "", text)

    if len(body) != TOTAL_DIGITS:
        return None
    if not damm_valid(body):
        return None
    return format_reference(body)


def looks_like_legacy(raw: str) -> bool:
    """The old batch-scoped codes, DMU-001-01, issued before this scheme.

    Six of them exist. They are still honoured by the redemption site, so keep
    being able to recognise one.
    """
    return bool(re.fullmatch(r"(?i)\s*dmu[-\s]?\d{3}[-\s]?\d{2}\s*", raw or ""))


def normalise_legacy(raw: str) -> str | None:
    if not looks_like_legacy(raw):
        return None
    body = digits_of(raw)
    return f"{PREFIX}-{body[0:3]}-{body[3:5]}"


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------

def build_pool(size: int = POOL_SIZE, seed: int | None = None) -> list[str]:
    """Draw the whole run of references in one go, in the order they go out.

    random.sample over the payload range gives distinct payloads without a
    retry loop, and because the sample is already in random order the pool is
    shuffled by construction. Distinct payloads mean distinct references: the
    check digit is a function of the payload, so it cannot collapse two.

    Pass a seed only to reproduce a pool for testing. The live pool is drawn
    once, from the system entropy source, and then never regenerated: the
    references are printed on paper and cannot be recalled.
    """
    span = PAYLOAD_HIGH - PAYLOAD_LOW + 1
    if size > span:
        raise ValueError(f"cannot draw {size:,} distinct references from {span:,}")

    rng = random.SystemRandom() if seed is None else random.Random(seed)
    payloads = rng.sample(range(PAYLOAD_LOW, PAYLOAD_HIGH + 1), size)
    return [make_reference(p) for p in payloads]
