"""Move a catalogue price, to demonstrate price-drift protection.

Quote a product, run this, then create the order from that quote: the
server re-reads the current price, sees it no longer matches the frozen
quote, invalidates the quote and refuses with PRICE_DRIFT. No order is
created and nothing is charged.

    python simulate_test_data.py                      # Smart Watch -> 1800
    python simulate_test_data.py "Gold Coin 5g" 5500  # any product, any price

The product is looked up BY NAME rather than by a hardcoded id. The id
that used to be pinned here belonged to one developer's database and
matched nothing in a freshly seeded one, so the script silently reported
"No product found" and the drift demo did nothing.
"""

import sys

from backend.database import SessionLocal
from backend.databases.product_db import product_db

DEFAULT_PRODUCT_NAME = "Smart Watch"
DEFAULT_NEW_PRICE = 1800.0


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PRODUCT_NAME
    new_price = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_NEW_PRICE

    db = SessionLocal()
    matches = (
        db.query(product_db).filter(product_db.product_name.ilike(name)).all()
    )

    if not matches:
        print(f"No product named {name!r}. Run seed_data.py first.")
        return

    if len(matches) > 1:
        # A seeded database holds one row per name. More than one means the
        # database has been seeded repeatedly, which is worth saying out
        # loud rather than silently repricing an arbitrary one.
        print(
            f"{len(matches)} products are named {name!r} -- this database has "
            "been seeded more than once. Recreate it before demonstrating "
            "price drift (see the README reset procedure)."
        )
        return

    product = matches[0]
    old_cost = product.cost
    product.cost = new_price
    db.commit()
    print(f"{product.product_name} ({product.product_id})")
    print(f"updated price from {old_cost} to {new_price}")


if __name__ == "__main__":
    main()
