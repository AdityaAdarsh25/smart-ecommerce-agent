"""Idempotent migration for the Package 3 mandate + approval work.

Adds, only if missing:

  * `products.category`                        (NULL for legacy rows)
  * `mandates.autonomous_limit`                (renamed from per_transaction_limit)
  * `mandates.absolute_transaction_limit`
  * the `mandate_allowed_merchants` table
  * the `mandate_allowed_categories` table
  * the `approvals` table

Conservative about history, in the same spirit as the Package 2 migration.
No transaction is re-decided and no approval is invented for a transaction
that is already AWAITING_APPROVAL from a previous run -- those rows predate
the approval record, and fabricating one would put words in a human's
mouth. They are reported instead.

`absolute_transaction_limit` has no honest legacy value, since the old
schema never expressed the idea. Existing mandates therefore get their
autonomous limit unchanged as the ceiling too:

    autonomous_limit           = previous per_transaction_limit
    absolute_transaction_limit = previous per_transaction_limit

That is the only conservative reading. A mandate whose owner never
expressed an approval band must not acquire one by being migrated -- any
multiplier would hand a human approver headroom nobody granted. Collapsing
the two limits leaves the mandate exactly as powerful as it was: every
amount it used to permit it still permits, and everything above is
blocked rather than newly approvable. Widening it is a policy decision and
belongs to whoever owns the mandate, not to a schema change.

Safe to re-run. Backs up to mandatepay.db.bak first.

    python migrate_package3.py
"""

import os
import shutil
import sqlite3

from backend.database import Base, engine

# Imported for their side effect: every table must be on the metadata
# before create_all can make the missing ones.
from backend.databases.approval_db import approval_db  # noqa: F401
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

    # Creates the new tables. Existing tables are left exactly as they are.
    Base.metadata.create_all(bind=engine)
    print("Ensured all tables exist (approvals and mandate permissions included)")

    if fresh:
        print("Fresh database created with the Package 3 schema -- nothing to migrate.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        # --- products.category ------------------------------------------
        if "category" in _columns(conn, "products"):
            print("products.category already present")
        else:
            conn.execute("ALTER TABLE products ADD COLUMN category VARCHAR")
            print("Added products.category (NULL for legacy rows)")

        # --- mandates ----------------------------------------------------
        mandate_columns = _columns(conn, "mandates")
        if "autonomous_limit" in mandate_columns:
            print("mandates.autonomous_limit already present")
        elif "per_transaction_limit" in mandate_columns:
            conn.execute(
                "ALTER TABLE mandates "
                "RENAME COLUMN per_transaction_limit TO autonomous_limit"
            )
            print("Renamed mandates.per_transaction_limit -> autonomous_limit")

        if "absolute_transaction_limit" in mandate_columns:
            print("mandates.absolute_transaction_limit already present")
        else:
            conn.execute(
                "ALTER TABLE mandates ADD COLUMN absolute_transaction_limit FLOAT"
            )
            # Deliberately equal to the autonomous limit: no legacy mandate
            # gains approval headroom it never granted.
            conn.execute(
                "UPDATE mandates SET absolute_transaction_limit = autonomous_limit "
                "WHERE absolute_transaction_limit IS NULL"
            )
            print(
                "Added mandates.absolute_transaction_limit, set equal to the "
                "autonomous limit -- no migrated mandate gains new authority"
            )
        conn.commit()

        # --- report, decide nothing --------------------------------------
        uncategorised = conn.execute(
            "SELECT COUNT(*) FROM products WHERE category IS NULL"
        ).fetchone()[0]
        print(
            f"\n{uncategorised} product(s) carry no category. A mandate that "
            "restricts categories will not permit them until one is set."
        )

        for mandate_id, autonomous, absolute, cap in conn.execute(
            "SELECT id, autonomous_limit, absolute_transaction_limit, monthly_cap "
            "FROM mandates"
        ):
            print(
                f"  mandate {mandate_id}: autonomous={autonomous}, "
                f"absolute={absolute}, monthly_cap={cap}"
            )

        print(
            "\nNo permission rows were created. An empty allow-list means "
            "unrestricted, so every existing mandate keeps the reach it had."
        )

        awaiting = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status = 'AWAITING_APPROVAL'"
        ).fetchone()[0]
        print(
            f"{awaiting} transaction(s) sit in AWAITING_APPROVAL from before "
            "approvals were recorded. No approval row was invented for them."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
