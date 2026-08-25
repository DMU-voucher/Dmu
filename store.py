"""The voucher database. Shared by the generator and the redemption site.

Two copies of this database exist and they are deliberately not the same thing:

  * the generator's copy, on the machine that prints vouchers. It owns the
    reference pool and decides which references have been handed out.
  * the redemption site's copy, on PythonAnywhere. It owns the redemptions.

The generator exports a batch, the site imports it. Nothing flows back
automatically, because the printing machine is somebody's laptop and the site
has to keep working when that laptop is shut. To reconcile, download the
redemptions CSV from the site's admin page and drop it into the generator.

SQLite rather than a CSV because the pool is half a million rows and allocation
has to be atomic: two vouchers must never get the same reference, even if the
app is started twice by accident.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import refs

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The 500,000 references, in the order they get handed out. seq is the draw
-- order, not the number itself, so allocation is "take the lowest free seq".
-- seq is the rowid, which costs nothing to index, and the partial index below
-- keeps "next free" instant however many have already gone out.
CREATE TABLE IF NOT EXISTS reference_pool (
    seq          INTEGER PRIMARY KEY,
    reference    TEXT NOT NULL UNIQUE,
    allocated_at TEXT,
    batch        TEXT
);

CREATE INDEX IF NOT EXISTS ix_pool_free
    ON reference_pool (seq) WHERE allocated_at IS NULL;

-- One row per voucher actually printed.
CREATE TABLE IF NOT EXISTS vouchers (
    reference    TEXT PRIMARY KEY,
    batch        TEXT NOT NULL,
    event_name   TEXT NOT NULL,
    cost_centre  TEXT,
    value_pence  INTEGER NOT NULL,
    valid_until  TEXT NOT NULL,
    event_date   TEXT,
    issued_at    TEXT NOT NULL,
    issued_by    TEXT,
    voided_at    TEXT,
    void_reason  TEXT
);

CREATE INDEX IF NOT EXISTS ix_vouchers_batch ON vouchers (batch);
CREATE INDEX IF NOT EXISTS ix_vouchers_event ON vouchers (event_name);

-- One row per redemption. A reversed redemption keeps its row and gains a
-- reversed_at, so an undo is visible rather than a gap in the record.
CREATE TABLE IF NOT EXISTS redemptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reference       TEXT NOT NULL,
    venue           TEXT NOT NULL,
    redeemed_at     TEXT NOT NULL,
    served_by       TEXT,
    note            TEXT,
    reversed_at     TEXT,
    reversed_by     TEXT,
    reversal_reason TEXT
);

CREATE INDEX IF NOT EXISTS ix_red_reference ON redemptions (reference);
CREATE INDEX IF NOT EXISTS ix_red_when      ON redemptions (redeemed_at);

-- Only one live redemption per voucher. A reversed row has reversed_at set and
-- drops out of the index, which is what lets a mistake be undone and the
-- voucher used properly afterwards.
CREATE UNIQUE INDEX IF NOT EXISTS ux_red_live
    ON redemptions (reference) WHERE reversed_at IS NULL;

-- Failed lookups, so guessing at references is throttled and visible.
CREATE TABLE IF NOT EXISTS lookup_failures (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tried  TEXT NOT NULL,
    reason TEXT NOT NULL,
    client TEXT,
    venue  TEXT,
    at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_fail_when ON lookup_failures (at);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    """Open the database.

    Deliberately not WAL. The generator's copy of this file lives in a Dropbox
    folder, and WAL leaves a -wal and a -shm alongside it that Dropbox syncs
    separately from the database they belong to. Getting those out of step is
    how a SQLite file gets corrupted. The rollback journal only exists during a
    write, and nothing here needs concurrent readers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    with conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    return conn


@contextmanager
def open_db(path: Path):
    conn = init(path)
    try:
        yield conn
    finally:
        conn.close()


def meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )


# ---------------------------------------------------------------------------
# The reference pool
# ---------------------------------------------------------------------------

def pool_size(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM reference_pool").fetchone()[0]


def pool_remaining(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM reference_pool WHERE allocated_at IS NULL"
    ).fetchone()[0]


def pool_used(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM reference_pool WHERE allocated_at IS NOT NULL"
    ).fetchone()[0]


def seed_pool(conn: sqlite3.Connection, size: int = refs.POOL_SIZE,
              seed: int | None = None, progress=None) -> int:
    """Fill the pool. Does nothing if it already has rows.

    Refusing to touch a pool that already exists is the whole point. Re-drawing
    it would reissue references that are already on printed vouchers, and the
    site would then show one voucher's details for another voucher's number.
    """
    existing = pool_size(conn)
    if existing:
        return 0

    if progress:
        progress(f"Drawing {size:,} references...")
    references = refs.build_pool(size, seed=seed)
    if progress:
        progress("Writing them down...")

    with conn:
        conn.executemany(
            "INSERT INTO reference_pool (seq, reference) VALUES (?, ?)",
            ((i, r) for i, r in enumerate(references, start=1)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('pool_drawn_at', ?)",
            (now(),),
        )
    conn.execute("VACUUM")  # once, while the file is still small enough to care
    return len(references)


class PoolExhausted(RuntimeError):
    pass


def allocate(conn: sqlite3.Connection, count: int, batch: str) -> list[str]:
    """Take the next few free references, in pool order.

    One transaction, so a crash halfway through hands out nothing rather than
    half a batch. IMMEDIATE takes the write lock up front so two copies of the
    app cannot both read the same free rows and then both claim them.
    """
    if count < 1:
        return []

    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT reference FROM reference_pool WHERE allocated_at IS NULL "
            "ORDER BY seq LIMIT ?",
            (count,),
        ).fetchall()

        if len(rows) < count:
            conn.rollback()
            raise PoolExhausted(
                f"Only {len(rows):,} references are left in the pool and "
                f"{count:,} are needed. Top the pool up before issuing more."
            )

        picked = [r["reference"] for r in rows]
        stamp = now()
        conn.executemany(
            "UPDATE reference_pool SET allocated_at = ?, batch = ? WHERE reference = ?",
            ((stamp, batch, r) for r in picked),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return picked


def release(conn: sqlite3.Connection, references: list[str]) -> None:
    """Put references back, for when PDF generation fails after allocation.

    Only ever called for references that were never printed.
    """
    if not references:
        return
    with conn:
        conn.executemany(
            "UPDATE reference_pool SET allocated_at = NULL, batch = NULL "
            "WHERE reference = ? AND allocated_at IS NOT NULL",
            ((r,) for r in references),
        )


# ---------------------------------------------------------------------------
# Vouchers
# ---------------------------------------------------------------------------

VOUCHER_FIELDS = ["reference", "batch", "event_name", "cost_centre",
                  "value_pence", "valid_until", "event_date", "issued_at",
                  "issued_by"]


def record_vouchers(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Write the printed vouchers in. Ignores ones already present, so
    re-importing the same batch export is safe."""
    with conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO vouchers "
            "(reference, batch, event_name, cost_centre, value_pence, "
            " valid_until, event_date, issued_at, issued_by) "
            "VALUES (:reference, :batch, :event_name, :cost_centre, "
            ":value_pence, :valid_until, :event_date, :issued_at, :issued_by)",
            [{k: r.get(k) for k in VOUCHER_FIELDS} for r in rows],
        )
        return cur.rowcount


def get_voucher(conn: sqlite3.Connection, reference: str):
    return conn.execute(
        "SELECT * FROM vouchers WHERE reference = ?", (reference,)
    ).fetchone()


def live_redemption(conn: sqlite3.Connection, reference: str):
    return conn.execute(
        "SELECT * FROM redemptions WHERE reference = ? AND reversed_at IS NULL",
        (reference,),
    ).fetchone()


def next_batch_number(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(CAST(batch AS INTEGER)) FROM vouchers").fetchone()
    return (row[0] or 0) + 1
