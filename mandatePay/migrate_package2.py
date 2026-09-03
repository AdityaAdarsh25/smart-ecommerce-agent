"""Idempotent migration for the Package 2 quote + payment-verification work.

Adds, only if missing:

  * the `quotes` table
  * `transactions.quote_id`         (NULL for pre-Package-2 rows)
  * `transactions.razorpay_payment_id`

Deliberately conservative about history. No existing row is reinterpreted:
a legacy ORDER_CREATED transaction had no verified payment, so it stays
ORDER_CREATED. Backfilling those to PAID would fabricate payments that
never happened, and would then be counted against the buyer's monthly cap.

Safe to re-run. Backs up to mandatepay.db.bak first.

    python migrate_package2.py
"""

import os
import shutil
import sqlite3

from backend.database import Base, engine

# Imported for their side effect: every table must be on the metadata
# before create_all can make the missing ones.
from backend.databases.buyer_db import buyer_db  # noqa: F401
from backend.databases.mandate_db import mandate_db  # noqa: F401
from backend.databases.merchant_db import merchant_db  # noqa: F401
from backend.databases.product_db import product_db  # noqa: F401
from backend.databases.quote_db import quote_db  # noqa: F401
from backend.databases.transaction_db import transaction_db  # noqa: F401

DB_PATH = "mandatepay.db"
BACKUP_PATH = "mandatepay.db.bak"

NEW_TRANSACTION_COLUMNS = {
    "quote_id": "CHAR(36)",
    "razorpay_payment_id": "VARCHAR",
}


def main() -> None:
    if os.path.exists(DB_PATH):
        shutil.copyfile(DB_PATH, BACKUP_PATH)
        print(f"Backed up {DB_PATH} -> {BACKUP_PATH}")

    # Creates `quotes` (and anything else missing). Existing tables are
    # left exactly as they are.
    Base.metadata.create_all(bind=engine)
    print("Ensured all tables exist (quotes included)")

    if not os.path.exists(DB_PATH):
        print("Fresh database created with the Package 2 schema -- nothing to migrate.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
        for name, sql_type in NEW_TRANSACTION_COLUMNS.items():
            if name in columns:
                print(f"transactions.{name} already present")
                continue
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {sql_type}")
            print(f"Added transactions.{name} (NULL for legacy rows)")
        conn.commit()

        legacy = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE quote_id IS NULL"
        ).fetchone()[0]
        print(f"\n{legacy} legacy transaction(s) carry no quote. Left untouched.")

        print("Status distribution (unchanged by this migration):")
        for status, count in conn.execute(
            "SELECT status, COUNT(*) FROM transactions GROUP BY status ORDER BY status"
        ):
            print(f"  {status:<20} {count}")

        paid = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status = 'PAID'"
        ).fetchone()[0]
        print(f"\nPAID rows: {paid} (this migration creates none)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
