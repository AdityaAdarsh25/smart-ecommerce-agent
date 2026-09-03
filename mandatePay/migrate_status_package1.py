"""One-shot migration for the Package 1 transaction-state rewrite.

The existing dev database holds status values from the old enum
(COMPLETED, PENDING_APPROVAL) that no longer exist, and predates the
`transactions.quantity` column. Loading those rows with the new enum
raises LookupError, so the local DB needs remapping once.

The remap is deliberately honest about what actually happened:

  COMPLETED        -> ORDER_CREATED     A Razorpay order was created and
                                        nothing was ever verified as paid.
                                        These were never real payments, so
                                        mapping them to PAID would bake the
                                        original bug into the data.
  PENDING_APPROVAL -> AWAITING_APPROVAL Straight rename.
  APPROVED         -> AUTHORIZED        Straight rename.
  BLOCKED          -> BLOCKED           Unchanged.
  CANCELLED        -> CANCELLED         Unchanged.

Safe to re-run. Backs up to mandatepay.db.bak first.

    python migrate_status_package1.py
"""

import os
import shutil
import sqlite3

DB_PATH = "mandatepay.db"
BACKUP_PATH = "mandatepay.db.bak"

STATUS_REMAP = {
    "COMPLETED": "ORDER_CREATED",
    "PENDING_APPROVAL": "AWAITING_APPROVAL",
    "APPROVED": "AUTHORIZED",
}


def main() -> None:
    if not os.path.exists(DB_PATH):
        print(f"No {DB_PATH} found -- nothing to migrate.")
        print("A fresh database from createtables.py already has the new schema.")
        return

    shutil.copyfile(DB_PATH, BACKUP_PATH)
    print(f"Backed up {DB_PATH} -> {BACKUP_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}

        if "quantity" not in columns:
            conn.execute("ALTER TABLE transactions ADD COLUMN quantity INTEGER")
            print("Added transactions.quantity (NULL for legacy rows)")
        else:
            print("transactions.quantity already present")

        total = 0
        for old, new in STATUS_REMAP.items():
            cur = conn.execute(
                "UPDATE transactions SET status = ? WHERE status = ?", (new, old)
            )
            if cur.rowcount:
                print(f"  {old:<18} -> {new:<18} {cur.rowcount} row(s)")
                total += cur.rowcount

        conn.commit()
        print(f"Remapped {total} row(s)")

        print("\nResulting status distribution:")
        for status, count in conn.execute(
            "SELECT status, COUNT(*) FROM transactions GROUP BY status ORDER BY status"
        ):
            print(f"  {status:<20} {count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
