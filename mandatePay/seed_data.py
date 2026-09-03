"""Seed the demo catalogue, demo buyer and their mandate.

There is no authentication in v1: the seeded buyer IS the demo identity,
and `buyer_id` is what a caller carries into a quote.

The mandate is shaped so all three headline outcomes are one request away,
and none of them is hardcoded to a product or merchant name -- every
decision falls out of the numbers and the permission rows below:

    autonomous_limit           3000    act alone at or below this
    absolute_transaction_limit 6000    no human may approve above this
    monthly_cap               20000    PAID spend this month

    2500 -> ALLOW              Weekly Grocery Hamper
    4000 -> REQUIRE_APPROVAL   Wireless Headphones
    7000 -> BLOCK              Gaming Laptop (over the absolute ceiling)

An EMPTY permission list means unrestricted; the lists below are non-empty
and therefore strict, which is what makes FurnitureHub and `furniture`
demonstrable blocks.
"""

from backend.database import SessionLocal
from backend.databases.buyer_db import buyer_db
from backend.databases.mandate_db import (
    mandate_allowed_category_db,
    mandate_allowed_merchant_db,
    mandate_db,
)
from backend.databases.merchant_db import merchant_db
from backend.databases.product_db import product_db

AUTONOMOUS_LIMIT = 3000.0
ABSOLUTE_TRANSACTION_LIMIT = 6000.0
MONTHLY_CAP = 20000.0
BALANCE = 50000.0


def main() -> None:
    db = SessionLocal()

    buyer = buyer_db(name="Arvind", balance=BALANCE)
    db.add(buyer)
    db.flush()

    mandate = mandate_db(
        buyer_id=buyer.id,
        autonomous_limit=AUTONOMOUS_LIMIT,
        absolute_transaction_limit=ABSOLUTE_TRANSACTION_LIMIT,
        monthly_cap=MONTHLY_CAP,
    )
    db.add(mandate)

    freshmart = merchant_db(name="FreshMart")
    techbazaar = merchant_db(name="TechBazaar")
    furniturehub = merchant_db(name="FurnitureHub")
    db.add_all([freshmart, techbazaar, furniturehub])
    db.flush()

    # Allowed merchants: FreshMart and TechBazaar. FurnitureHub is not on
    # the list, so every product it sells is a merchant block.
    db.add_all(
        [
            mandate_allowed_merchant_db(mandate_id=mandate.id, merchant_id=freshmart.id),
            mandate_allowed_merchant_db(
                mandate_id=mandate.id, merchant_id=techbazaar.id
            ),
        ]
    )

    # Allowed categories: groceries and electronics. `furniture` and
    # `jewellery` are not, so those are category blocks even when sold by
    # an allowed merchant.
    db.add_all(
        [
            mandate_allowed_category_db(mandate_id=mandate.id, category="groceries"),
            mandate_allowed_category_db(mandate_id=mandate.id, category="electronics"),
        ]
    )

    products = [
        # Autonomous ALLOW: 2500, under the 3000 threshold.
        product_db(
            product_name="Weekly Grocery Hamper",
            brand="FreshMart",
            cost=2500.0,
            category="groceries",
            quantity=40,
            merchant_id=freshmart.id,
        ),
        product_db(
            product_name="Mixed Fruit Basket",
            brand="FreshMart",
            cost=200.0,
            category="groceries",
            quantity=50,
            merchant_id=freshmart.id,
        ),
        # Human APPROVAL: 4000, between 3000 and 6000.
        product_db(
            product_name="Wireless Headphones",
            brand="boAt",
            cost=4000.0,
            category="electronics",
            quantity=20,
            merchant_id=techbazaar.id,
        ),
        # Hard BLOCK: 7000, above the 6000 absolute ceiling. No human may
        # approve this one.
        product_db(
            product_name="Gaming Laptop",
            brand="ASUS",
            cost=7000.0,
            category="electronics",
            quantity=5,
            merchant_id=techbazaar.id,
        ),
        # Merchant block: FurnitureHub is not a permitted merchant (and
        # furniture is not a permitted category either).
        product_db(
            product_name="Portable Table",
            brand="Woodland",
            cost=400.0,
            category="furniture",
            quantity=20,
            merchant_id=furniturehub.id,
        ),
        # Category block only: an allowed merchant selling a category the
        # mandate does not permit.
        product_db(
            product_name="Gold Coin 5g",
            brand="Kalyan",
            cost=5000.0,
            category="jewellery",
            quantity=10,
            merchant_id=techbazaar.id,
        ),
        # Monthly cap demo: 5 of these is 22500, past the 20000 cap.
        product_db(
            product_name="Bluetooth Speaker",
            brand="JBL",
            cost=4500.0,
            category="electronics",
            quantity=30,
            merchant_id=techbazaar.id,
        ),
        # Price drift demo: quote it, then move the price with
        # simulate_test_data.py before creating the order.
        product_db(
            product_name="Smart Watch",
            brand="Noise",
            cost=1200.0,
            category="electronics",
            quantity=15,
            merchant_id=techbazaar.id,
        ),
    ]
    db.add_all(products)
    db.commit()

    print("Seed data created successfully")
    print(f"buyer: {buyer.name} (id={buyer.id}, balance={buyer.balance})")
    print(
        f"mandate: autonomous={mandate.autonomous_limit}, "
        f"absolute={mandate.absolute_transaction_limit}, "
        f"monthly_cap={mandate.monthly_cap}"
    )
    print(f"allowed merchants: FreshMart={freshmart.id}, TechBazaar={techbazaar.id}")
    print(f"disallowed merchant: FurnitureHub={furniturehub.id}")
    print("allowed categories: groceries, electronics")
    print("disallowed categories: furniture, jewellery")
    for p in products:
        print(
            f"product: {p.product_name} (id={p.product_id}, category={p.category}, "
            f"cost={p.cost}, stock={p.quantity}, merchant_id={p.merchant_id})"
        )


if __name__ == "__main__":
    main()
