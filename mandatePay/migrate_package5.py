"""Idempotent migration for the Package 5 audit trail.

Adds, only if missing:

  * the `audit_events` table

That is the entire schema change. Nothing else is touched.

HISTORY IS NOT INVENTED. Transactions, quotes and approvals that predate
this table have no audit events and will not get any. Fabricating a
QUOTE_CREATED or a TRANSACTION_PAID for a row whose actual history nobody
recorded would be the exact failure an audit trail exists to prevent -- a
plausible-looking record of something that was never observed. Old rows
therefore read back with an empty event list, which is the honest answer:
we do not know what happened, because we were not writing it down.

The same applies to approvals. No APPROVAL_APPROVED event is written for
an already-approved historical transaction; the approval row itself
remains the record of what the human said, and putting a second,
after-the-fact "event" beside it would be putting words in their mouth.

Safe to re-run. Backs up to mandatepay.db.bak first.

    python migrate_package5.py
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


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def main() -> None:
    fresh = not os.path.exists(DB_PATH)
    if not fresh:
        shutil.copyfile(DB_PATH, BACKUP_PATH)
        print(f"Backed up {DB_PATH} -> {BACKUP_PATH}")

    existed = False
    if not fresh:
        probe = sqlite3.connect(DB_PATH)
        try:
            existed = _table_exists(probe, "audit_events")
        finally:
            probe.close()

    Base.metadata.create_all(bind=engine)
    if existed:
        print("audit_events already present -- nothing to create")
    else:
        print("Created audit_events (empty)")

    if fresh:
        print("Fresh database created with the Package 5 schema.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        events = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        transactions = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        quotes = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        approvals = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]

        print(f"\naudit_events holds {events} event(s).")
        print(
            f"{transactions} transaction(s), {quotes} quote(s) and "
            f"{approvals} approval(s) predate the audit trail."
        )
        print(
            "No historical event was invented for any of them. Their audit "
            "endpoint returns an empty list, which is the truthful answer: "
            "nothing observed them at the time."
        )
        print(
            "\nNo payment history was fabricated, no transaction was "
            "re-decided, and no existing row was modified by this migration."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
