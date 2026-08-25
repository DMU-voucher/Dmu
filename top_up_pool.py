"""Add more voucher numbers to the pool, without disturbing the ones drawn.

Only needed if the original 500,000 ever run out. At the rate these vouchers go
out that is not this decade, but the app refuses to issue past the end of the
pool, so there has to be a way through.

    python top_up_pool.py            see how many are left
    python top_up_pool.py 100000     add another hundred thousand

Run it from the generator folder with the app closed.

What it does NOT do is redraw the pool. Redrawing would hand out numbers that
are already printed on paper, and the site would then show one voucher's details
for another voucher's number. New numbers are appended after the existing ones
and checked against them first, so nothing already issued can reappear.
"""

from __future__ import annotations

import sys

import refs
import store
import vouchers as core


def main(argv: list[str]) -> int:
    conn = store.init(core.DB_PATH)
    try:
        total = store.pool_size(conn)
        used = store.pool_used(conn)
        print()
        print(f"  Pool: {total:,} numbers, {used:,} issued, {total - used:,} left.")

        if len(argv) < 2:
            print()
            print("  To add more:  python top_up_pool.py 100000")
            print()
            return 0

        try:
            wanted = int(argv[1].replace(",", "").replace("_", ""))
        except ValueError:
            print(f"\n  '{argv[1]}' is not a number of vouchers.\n")
            return 1

        if wanted < 1:
            print("\n  Nothing to do.\n")
            return 1

        print(f"  Drawing {wanted:,} more...")
        existing = {r["reference"] for r in
                    conn.execute("SELECT reference FROM reference_pool")}

        # Drawn against the same 90 million, so collisions with what is already
        # in the pool are expected and simply discarded. Draw generously and
        # trim, rather than looping until the count comes out exactly.
        candidates = refs.build_pool(min(wanted * 2, refs.PAYLOAD_HIGH - refs.PAYLOAD_LOW))
        fresh = []
        for reference in candidates:
            if reference not in existing:
                fresh.append(reference)
                if len(fresh) == wanted:
                    break

        if len(fresh) < wanted:
            print(f"  Only {len(fresh):,} new numbers were available.")

        with conn:
            conn.executemany(
                "INSERT INTO reference_pool (seq, reference) VALUES (?, ?)",
                ((total + i, r) for i, r in enumerate(fresh, start=1)),
            )

        print(f"  Added {len(fresh):,}. The pool now holds "
              f"{store.pool_size(conn):,}, with "
              f"{store.pool_remaining(conn):,} unused.")
        print()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
