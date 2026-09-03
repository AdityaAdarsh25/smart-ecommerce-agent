"""Idempotent migration for the settlement-timestamp safety correction.

Adds, only if missing:

  * `transactions.paid_at`   (NULL for everything not verified-paid)

WHY THE COLUMN EXISTS

`transactions.timestamp` records when a transaction was CREATED and never
changes. The monthly cap asks a different question -- how much has this
buyer actually PAID this month -- and answering it from the creation
timestamp misfiles every purchase that straddles a month boundary: an
order created on 31 August and settled on 1 September was being counted
against August, a month whose allowance it did not spend.

`paid_at` is the authoritative settlement time. It is written exactly
once, in the same database transaction that writes PAID, and stays NULL
for everything else.

WHAT IS BACKFILLED, AND WHAT IS NOT

Only rows already in PAID get a value, and the only value available is
their creation timestamp:

    paid_at = timestamp   for status = PAID

That is a conservative approximation, not a discovery. The true
settlement time of a historical payment was never recorded and cannot be
recovered; using the creation timestamp keeps those payments counting
against a monthly cap in the month they were most likely settled, rather
than dropping out of every cap because the column is NULL. Nothing else
is touched: no transaction is re-decided, no status changes, no balance
or stock moves, and no unpaid row is given a settlement time it never
had.

A row that somehow reaches PAID with a NULL `paid_at` still counts
against the cap regardless -- `_monthly_spend` falls back to the creation
timestamp -- so a partially migrated database can under-report a buyer's
allowance but never over-report it.

Safe to re-run. Backs up to mandatepay.db.bak first.

    python migrate_package6.py
"""

import os
import shutil
import sqlite3

from backend.database import Base, engine

# Imported for their side effect: every table must be on the metadata
# before create_all can make the missing ones.
from backend.databases.approval_db import approval_db  # noqa: F401
from backend.databases.audit_db import audit_event_db  # noqa: F401
from backend.databases.buyer_db import buyer_db  # noqa: F401
from backend.databases.mandate_db import (  # noqa: F401
    mandate_allowed_category_db,
    mandate_allowed_merchant_db,
    mandate_db,
)
from backend.databases.merchant_db import merchant_db  # noqa: F401
from backend.databases.product_db import product_db  # noqa: F401
from backend.databases.quote_db import quote_db  # noqa: F401
from backend.databases.transaction_db import transaction_db  # noqa: F401

DB_PATH = "mandatepay.db"
BACKUP_PATH = "mandatepay.db.bak"


def _columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def main() -> None:
    fresh = not os.path.exists(DB_PATH)
    if not fresh:
        shutil.copyfile(DB_PATH, BACKUP_PATH)
        print(f"Backed up {DB_PATH} -> {BACKUP_PATH}")

    # A fresh database gets `paid_at` from the model itself; an existing
    # one keeps every table it already has.
    Base.metadata.create_all(bind=engine)

    if fresh:
        print("Fresh database created with the paid_at schema -- nothing to migrate.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        if "paid_at" in _columns(conn, "transactions"):
            print("transactions.paid_at already present")
        else:
            conn.execute("ALTER TABLE transactions ADD COLUMN paid_at DATETIME")
            print("Added transactions.paid_at (NULL for every existing row)")

        backfilled = conn.execute(
            "UPDATE transactions SET paid_at = timestamp "
            "WHERE status = 'PAID' AND paid_at IS NULL"
        ).rowcount
        conn.commit()

        print(
            f"Backfilled paid_at on {backfilled} historical PAID "
            "transaction(s) from their creation timestamp."
        )
        if backfilled:
            print(
                "That value is an approximation and is labelled as one here: "
                "the real settlement time of those payments was never "
                "recorded, so it cannot be recovered. It is used because a "
                "paid transaction that counts against no month at all would "
                "quietly hand its buyer back allowance they had spent."
            )

        unpaid = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE status != 'PAID' AND paid_at IS NOT NULL"
        ).fetchone()[0]
        print(
            f"{unpaid} unpaid transaction(s) carry a settlement time "
            "(expected: 0 -- only a verified payment may write one)."
        )

        paid = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status = 'PAID'"
        ).fetchone()[0]
        still_null = conn.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE status = 'PAID' AND paid_at IS NULL"
        ).fetchone()[0]
        print(
            f"\n{paid} PAID transaction(s) in total, {still_null} of them "
            "still without a settlement time."
        )
        print(
            "No transaction was re-decided, no status was changed, and no "
            "balance or stock was moved by this migration."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
