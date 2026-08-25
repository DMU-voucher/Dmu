"""Move the voucher pool, and everything issued from it, to another machine.

The pool cannot be regenerated. It is drawn once from system entropy with no
seed recorded, because the references end up printed on paper and cannot be
recalled. So moving the app to a server means moving this file, not remaking it.

The whole database is 29.7 MB, which is a big and fragile browser upload. This
carries the same state in about 2.8 MB by writing the pool as gzipped text.

On the machine that has the pool:

    python move_pool.py export

That writes pool-transfer.jsonl.gz. Upload it, then on the new machine:

    python3.10 move_pool.py import pool-transfer.jsonl.gz --into ~/dmu-voucher-data/vouchers.db

Whatever path is used there has to be the one DMU_DB_PATH points at on the
server. The site creates an empty database at whatever path it is given, so a
mismatch produces no error, just a site where no voucher is ever recognised.

The import refuses to touch a database that already has a pool, for the same
reason the generator refuses to redraw one.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from pathlib import Path

import store
import vouchers as core

DEFAULT_EXPORT = Path(__file__).resolve().parent / "pool-transfer.jsonl.gz"


def export(out_path: Path) -> int:
    src = core.DB_PATH
    if not src.is_file():
        print(f"  No pool here. Looked for {src}")
        return 1

    conn = sqlite3.connect(str(src))
    conn.row_factory = sqlite3.Row
    try:
        pool = conn.execute(
            "SELECT seq, reference, allocated_at, batch FROM reference_pool ORDER BY seq"
        ).fetchall()
        vouchers = conn.execute("SELECT * FROM vouchers ORDER BY issued_at, reference").fetchall()
        meta = conn.execute("SELECT key, value FROM meta").fetchall()
    finally:
        conn.close()

    used = sum(1 for r in pool if r["batch"])
    print(f"  Pool:     {len(pool):,} references, {used:,} already issued")
    print(f"  Vouchers: {len(vouchers):,} rows")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(json.dumps({
            "kind": "dmu-pool-transfer", "version": 1,
            "pool_rows": len(pool), "voucher_rows": len(vouchers),
            "meta": {r["key"]: r["value"] for r in meta},
        }) + "\n")
        for r in pool:
            # A list, not an object: 500,000 copies of the key names is 20 MB
            # of nothing.
            fh.write(json.dumps([r["seq"], r["reference"], r["allocated_at"], r["batch"]]) + "\n")
        fh.write(json.dumps({"section": "vouchers"}) + "\n")
        for r in vouchers:
            fh.write(json.dumps(dict(r)) + "\n")

    mb = out_path.stat().st_size / 1048576
    print(f"  Written:  {out_path}  ({mb:.1f} MB)")
    print()
    print("  Upload that file to the new machine, then run the import there.")
    return 0


def do_import(src_path: Path, into: Path) -> int:
    if not src_path.is_file():
        print(f"  No such file: {src_path}")
        return 1

    conn = store.init(into)
    try:
        if store.pool_size(conn):
            print(f"  {into} already has a pool of {store.pool_size(conn):,}.")
            print("  Refusing to touch it. Move that file aside first if you are sure.")
            return 1

        with gzip.open(src_path, "rt", encoding="utf-8") as fh:
            header = json.loads(fh.readline())
            if header.get("kind") != "dmu-pool-transfer":
                print("  That is not a pool transfer file.")
                return 1
            print(f"  Expecting {header['pool_rows']:,} pool rows, "
                  f"{header['voucher_rows']:,} vouchers")

            pool_rows, voucher_rows, section = [], [], "pool"
            for line in fh:
                rec = json.loads(line)
                if isinstance(rec, dict) and rec.get("section") == "vouchers":
                    section = "vouchers"
                    continue
                (pool_rows if section == "pool" else voucher_rows).append(rec)

        if len(pool_rows) != header["pool_rows"]:
            print(f"  Truncated: got {len(pool_rows):,} pool rows, "
                  f"expected {header['pool_rows']:,}. Upload it again.")
            return 1

        with conn:
            conn.executemany(
                "INSERT INTO reference_pool (seq, reference, allocated_at, batch) "
                "VALUES (?, ?, ?, ?)", pool_rows)
            for v in voucher_rows:
                cols = ", ".join(v.keys())
                marks = ", ".join("?" * len(v))
                conn.execute(f"INSERT OR IGNORE INTO vouchers ({cols}) VALUES ({marks})",
                             list(v.values()))
            for key, value in (header.get("meta") or {}).items():
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                             (key, value))

        used = conn.execute(
            "SELECT COUNT(*) FROM reference_pool WHERE batch IS NOT NULL").fetchone()[0]
        total = store.pool_size(conn)
        vcount = conn.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0]
        print(f"  Done. {total:,} references, {used:,} issued, {vcount:,} vouchers.")
        print(f"  Database: {into}")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", type=Path, default=DEFAULT_EXPORT)
    i = sub.add_parser("import")
    i.add_argument("file", type=Path)
    i.add_argument("--into", type=Path, required=True)
    args = ap.parse_args()
    print()
    rc = export(args.out) if args.cmd == "export" else do_import(args.file, args.into)
    print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
